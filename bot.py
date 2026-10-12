#!/usr/bin/env python3
"""HubSpot Assistant Slack bot.

  @HubSpot Assistant <question>   in a channel → answers in a thread
  DM the bot                       → answers in the DM
  Reply in a thread the bot is in  → follow-up (no @ needed; it remembers the thread)
  Changes (update / note / task)   → posted as a card with [Approve] [Cancel]; HubSpot changes only on Approve

Runs over Socket Mode (no public URL needed). Reads SLACK_BOT_TOKEN, SLACK_APP_TOKEN, ANTHROPIC_API_KEY,
HUBSPOT_TOKEN and optional ALLOWED_WRITERS from .env. Every approved change is logged to audit.jsonl.
"""
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env():
    env = ROOT / ".env"
    if not env.exists():
        raise SystemExit("Missing .env. Copy .env.example to .env and fill in the keys (see README.md).")
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


load_env()

from slack_bolt import App  # noqa: E402  (imported after .env is loaded)
from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402

import agent  # noqa: E402
import hubspot  # noqa: E402

app = App(token=os.environ["SLACK_BOT_TOKEN"])
BOT_USER_ID = app.client.auth_test()["user_id"]
# Slack user IDs allowed to propose/approve changes. Empty = everyone in the workspace.
WRITERS = {u.strip() for u in os.environ.get("ALLOWED_WRITERS", "").split(",") if u.strip()}

PENDING_FILE = ROOT / "pending.json"   # proposals waiting for Approve/Cancel (survives restarts)
AUDIT_FILE = ROOT / "audit.jsonl"      # one line per approved/cancelled/failed change
HISTORY = {}                           # (channel, thread_ts) → Claude message list. Lost on restart.
THREAD_LOCKS = {}
LOCK = threading.Lock()
USER_CACHE = {}


# ---------- helpers ----------

def can_write(user_id):
    return not WRITERS or user_id in WRITERS


def who(user_id):
    """'Name (email)' for a Slack user, so 'my deals' can be matched to a HubSpot owner."""
    if user_id not in USER_CACHE:
        try:
            u = app.client.users_info(user=user_id)["user"]
            USER_CACHE[user_id] = f"{u.get('real_name') or u.get('name')} ({u.get('profile', {}).get('email', 'no email')})"
        except Exception:
            USER_CACHE[user_id] = user_id
    return USER_CACHE[user_id]


def load_pending():
    try:
        return json.loads(PENDING_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_pending(pending):
    PENDING_FILE.write_text(json.dumps(pending, indent=2))


def audit(event, proposal, user_id, result=None):
    entry = {"at": datetime.now(timezone.utc).isoformat(), "event": event, "by": user_id,
             "by_name": who(user_id), "requested_by": proposal.get("requested_by"),
             "proposal": {k: v for k, v in proposal.items() if k not in ("channel", "thread_ts")},
             "result": result}
    with AUDIT_FILE.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def describe(p):
    """Slack text for a proposal card."""
    if p["tool"] == "create_record":
        lines = [f"*Proposed new {p['object_type'][:-1]}: {p['record_name']}*"]
        lines += [f"• `{k}`: *{v}*" for k, v in p["properties"].items()]
        for a in p.get("associate", []):
            lines.append(f"• Linked to {a['object_type'][:-1]}: {a.get('record_name') or a['record_id']}")
        return "\n".join(lines)
    url = hubspot.record_url(p["object_type"], p["record_id"])
    name = f"<{url}|{p['record_name']}>" if url else f"*{p['record_name']}*"
    if p["tool"] == "update_record":
        lines = [f"*Proposed change to {name}* ({p['object_type'][:-1]})"]
        if p.get("summary"):
            lines.append(p["summary"])
        for k, v in p["properties"].items():
            before = p["current_values"].get(k)
            lines.append(f"• `{k}`: {before + ' → ' if before else ''}*{v}*")
    elif p["tool"] == "create_note":
        lines = [f"*Proposed note on {name}*", f">{p['body'][:1500]}"]
    else:
        lines = [f"*Proposed task on {name}*", f"• Subject: *{p['subject']}*"]
        for label, key in (("Due", "due_date"), ("Priority", "priority"), ("Owner ID", "owner_id"), ("Details", "body")):
            if p.get(key):
                lines.append(f"• {label}: {p[key]}")
    return "\n".join(lines)


def post_proposal(p, channel, thread_ts, user_id):
    p.update(channel=channel, thread_ts=thread_ts, requested_by=user_id)
    with LOCK:
        pending = load_pending()
        pending[p["id"]] = p
        save_pending(pending)
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=describe(p), blocks=[
        {"type": "section", "text": {"type": "mrkdwn", "text": describe(p)[:2900]}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary",
             "action_id": "approve_change", "value": p["id"]},
            {"type": "button", "text": {"type": "plain_text", "text": "Cancel"},
             "action_id": "cancel_change", "value": p["id"]}]},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": "Nothing changes in HubSpot until someone clicks Approve."}]},
    ])


def note_for_thread(channel, thread_ts, text):
    """Tell Claude (next time this thread is used) what happened to a proposal."""
    with LOCK:
        history = HISTORY.get((channel, thread_ts))
        thread_lock = THREAD_LOCKS.get((channel, thread_ts))
    if history is not None and thread_lock is not None:
        with thread_lock:  # wait if Claude is mid-answer in this thread
            history.append({"role": "user", "content": f"[Update from the Slack app, not the user: {text}]"})


# ---------- answering ----------

def handle(channel, thread_ts, user_id, text, reply_ts):
    text = re.sub(rf"<@{BOT_USER_ID}>", "", text or "").strip()
    if not text:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text="Ask me anything about HubSpot, e.g. _what stage is the Acme deal in?_")
        return
    key = (channel, thread_ts)
    with LOCK:
        history = HISTORY.setdefault(key, [])
        thread_lock = THREAD_LOCKS.setdefault(key, threading.Lock())
    try:
        app.client.reactions_add(channel=channel, timestamp=reply_ts, name="eyes")
    except Exception:
        pass
    with thread_lock:  # one question at a time per thread, so history stays in order
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            reply, proposals = agent.ask(
                history, f"[Asked by {who(user_id)} on {today}]\n{text}", can_write=can_write(user_id))
        except Exception as e:  # never leave the user hanging silently
            reply, proposals = f":warning: Something went wrong: `{type(e).__name__}: {str(e)[:300]}`", []
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=reply[:39000])
    for p in proposals:
        post_proposal(p, channel, thread_ts, user_id)
    try:
        app.client.reactions_remove(channel=channel, timestamp=reply_ts, name="eyes")
    except Exception:
        pass


def start(*args):
    threading.Thread(target=handle, args=args, daemon=True).start()


@app.event("app_mention")
def on_mention(event):
    thread_ts = event.get("thread_ts") or event["ts"]
    start(event["channel"], thread_ts, event["user"], event.get("text", ""), event["ts"])


@app.event("message")
def on_message(event):
    if event.get("bot_id") or event.get("subtype") or not event.get("user"):
        return
    if f"<@{BOT_USER_ID}>" in event.get("text", "") and event.get("channel_type") != "im":
        return  # already handled by on_mention
    if event.get("channel_type") == "im":
        # In DMs, each top-level message starts a thread so conversations stay separate.
        thread_ts = event.get("thread_ts") or event["ts"]
    elif event.get("thread_ts") and (event["channel"], event["thread_ts"]) in HISTORY:
        thread_ts = event["thread_ts"]  # follow-up in a thread the bot is already in
    else:
        return
    start(event["channel"], thread_ts, event["user"], event.get("text", ""), event["ts"])


# ---------- approvals ----------

def _finish_card(body, text):
    app.client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=text,
                           blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}])


@app.action("approve_change")
def approve(ack, body):
    ack()
    user_id, pid = body["user"]["id"], body["actions"][0]["value"]
    if not can_write(user_id):
        app.client.chat_postEphemeral(channel=body["channel"]["id"], user=user_id,
                                      text="You're not on the list of people who can approve HubSpot changes.")
        return
    with LOCK:  # pop first so a double-click can't apply the change twice
        pending = load_pending()
        p = pending.pop(pid, None)
        save_pending(pending)
    if not p:
        return
    try:
        result = agent.apply_proposal(p)
    except Exception as e:
        audit("failed", p, user_id, str(e))
        _finish_card(body, f"{describe(p)}\n\n:x: *Failed*, nothing was changed: `{str(e)[:500]}`")
        note_for_thread(p["channel"], p["thread_ts"], f"proposal {pid} was approved but HubSpot rejected it: {e}")
        return
    audit("approved", p, user_id, result)
    link = f" <{result['url']}|Open in HubSpot>" if p["tool"] == "create_record" and result.get("url") else ""
    _finish_card(body, f"{describe(p)}\n\n:white_check_mark: *Approved by <@{user_id}> and saved to HubSpot.*{link}")
    note_for_thread(p["channel"], p["thread_ts"], f"proposal {pid} was approved and saved to HubSpot.")


@app.action("cancel_change")
def cancel(ack, body):
    ack()
    user_id, pid = body["user"]["id"], body["actions"][0]["value"]
    with LOCK:
        pending = load_pending()
        p = pending.pop(pid, None)
        save_pending(pending)
    if not p:
        return
    audit("cancelled", p, user_id)
    _finish_card(body, f"~{describe(p)}~\n\n:no_entry_sign: *Cancelled by <@{user_id}>.* Nothing was changed.")
    note_for_thread(p["channel"], p["thread_ts"], f"proposal {pid} was cancelled; HubSpot was not changed.")


if __name__ == "__main__":
    print(f"HubSpot Assistant running as {BOT_USER_ID}. Writers: {', '.join(WRITERS) or 'everyone'}")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
