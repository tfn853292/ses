# main.py
import os
import time
import json
import logging
from instagrapi import Client
from instagrapi.exceptions import ClientError, ClientLoginRequired

# ========== CONFIG ==========
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "8"))   # seconds between inbox polls (set >= 5)
REPLY_TEXT    = os.environ.get("REPLY_TEXT", "Hello! Thanks for the message. 👋")
SESSION_FILE  = "session.json"
SEEN_FILE     = "seen_messages.json"
# ============================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ig-bot")

def write_seen(seen_dict):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(seen_dict, f)
    except Exception as e:
        log.warning("Could not write seen file: %s", e)

def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def ensure_session_file_from_env():
    """
    If running on Render and you put the session.json content into
    the env var SESSION_JSON, create a session.json file from it.
    """
    if "SESSION_JSON" in os.environ:
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                f.write(os.environ["SESSION_JSON"])
            log.info("Created session.json from SESSION_JSON environment variable.")
        except Exception as e:
            log.error("Failed to write session.json from env: %s", e)

def create_client():
    cl = Client()
    # load local session file if exists
    try:
        cl.load_settings(SESSION_FILE)
        log.info("Loaded session.json from disk.")
    except Exception as e:
        log.info("No session.json loaded from disk (or load failed): %s", e)
    return cl

def try_login_with_session(cl):
    try:
        # This will use the loaded session. If invalid it may raise errors.
        cl.login(cl.username or "", cl.password or "")
    except Exception:
        # It's OK to ignore here; we'll detect invalid session later by trying to fetch info
        pass

def get_threads_safe(cl):
    """
    Try multiple read methods because instagrapi API names vary slightly across versions.
    Return: list of thread-like objects or dicts with id and last message id and participants count.
    We'll normalize to a list of dicts: {"thread_id": str, "last_msg_id": str or int, "participants": int}
    """
    threads_normalized = []
    try:
        # Preferred: direct_threads() returns objects with .id and .items[0].id often
        threads = cl.direct_threads()
        for t in threads:
            thread_id = getattr(t, "id", None) or getattr(t, "thread_id", None)
            last_msg_id = None
            # items might be a list of messages
            items = getattr(t, "items", None) or getattr(t, "messages", None) or []
            if items:
                first = items[0]
                last_msg_id = getattr(first, "id", None) or first.get("id") if isinstance(first, dict) else None
            # users / participants
            users = getattr(t, "users", None) or getattr(t, "participants", None) or []
            participants = len(users) if users else 0
            if thread_id:
                threads_normalized.append({
                    "thread_id": str(thread_id),
                    "last_msg_id": str(last_msg_id) if last_msg_id is not None else None,
                    "participants": participants
                })
        if threads_normalized:
            return threads_normalized
    except Exception:
        # fallback below
        pass

    # fallback: try direct_v2_inbox (returns dict)
    try:
        inbox = cl.direct_v2_inbox()
        threads = inbox.get("inbox", {}).get("threads", []) if isinstance(inbox, dict) else []
        for t in threads:
            thread_id = t.get("thread_id") or t.get("id")
            last_msg_id = None
            items = t.get("items") or []
            if items:
                last_msg_id = items[0].get("item_id") or items[0].get("id")
            participants = len(t.get("users") or [])
            if thread_id:
                threads_normalized.append({
                    "thread_id": str(thread_id),
                    "last_msg_id": str(last_msg_id) if last_msg_id is not None else None,
                    "participants": participants
                })
        return threads_normalized
    except Exception:
        pass

    # If still nothing, return empty list
    return threads_normalized

def send_reply(cl, thread_id, text):
    try:
        # direct_send is the standard method: direct_send(text, thread_ids)
        # instagrapi accepts either a single thread_id or list
        cl.direct_send(text, [thread_id])
        log.info("Sent reply to thread %s", thread_id)
    except Exception as e:
        log.error("Failed to send message to %s : %s", thread_id, e)

def main_loop():
    ensure_session_file_from_env()
    cl = create_client()

    # Confirm login / session validity
    try:
        try_login_with_session(cl)
        # Try to fetch current user to ensure session works
        if not getattr(cl, "username", None):
            # try to set username if session saved it
            settings = cl.get_settings() if hasattr(cl, "get_settings") else None
            if settings:
                cl.username = settings.get("username") or settings.get("client_settings", {}).get("username")
        me = None
        if cl.username:
            try:
                me = cl.user_info_by_username(cl.username)
            except Exception:
                # try generic user_info
                try:
                    me = cl.user_info()
                except Exception:
                    me = None
        else:
            try:
                me = cl.user_info()
            except Exception:
                me = None

        if not me:
            log.error("Could not verify session — user info not available. Make sure session.json is valid.")
        else:
            log.info("Logged in as: %s (pk=%s)", getattr(me, "username", None), getattr(me, "pk", None))
    except Exception as e:
        log.error("Login verification error: %s", e)

    seen = load_seen()

    log.info("Starting polling loop (interval %s sec).", POLL_INTERVAL)
    while True:
        try:
            threads = get_threads_safe(cl)
            if not threads:
                log.debug("No threads returned on this poll.")
            for t in threads:
                tid = t["thread_id"]
                last = t["last_msg_id"]
                participants = t["participants"]

                # Consider it a group if participants > 2 (change if you want different rule)
                is_group = participants > 2

                # If we don't know last message id, skip
                if last is None:
                    continue

                # If we have never seen this thread, record last and skip replying to old messages
                if tid not in seen:
                    seen[tid] = last
                    continue

                # If last message id changed and it's newer than seen -> reply
                if seen.get(tid) != last:
                    # update seen first to avoid double send on errors
                    seen[tid] = last
                    write_seen(seen)
                    if is_group:
                        log.info("New message in group thread %s (last=%s). Sending reply.", tid, last)
                        send_reply(cl, tid, REPLY_TEXT)
                    else:
                        log.info("New message in non-group thread %s — ignoring (participants=%s).", tid, participants)

            # sleep to avoid rate limits
            time.sleep(POLL_INTERVAL)

        except ClientLoginRequired:
            log.error("Login required — the session may have expired. Exiting.")
            break
        except Exception as e:
            log.exception("Unexpected error during poll loop: %s", e)
            # backoff a bit on unexpected errors
            time.sleep(max(5, POLL_INTERVAL))

if __name__ == "__main__":
    log.info("Instagram auto-reply bot starting...")
    main_loop()
