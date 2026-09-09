"""applescript_send.py — send iMessage texts via Messages.app + AppleScript.

Draft+confirm pattern matches whatsapp-mcp:
  - send_message(...) returns a draft_id; nothing leaves the machine yet.
  - confirm_send(draft_id) commits, runs the AppleScript, and returns success.

Drafts are stored in-memory inside the FastMCP process. Drafts auto-expire
after 1 hour. Each draft can be confirmed at most once.

mark_chat_read(chat_guid) is a one-step call (lower consequence) and bypasses
the draft pattern.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import time
from dataclasses import dataclass
from typing import Any

DRAFT_TTL_SEC = 3600.0
_DRAFTS: dict[str, "Draft"] = {}


@dataclass
class Draft:
    draft_id: str
    recipient: str
    service: str  # "iMessage" | "SMS"
    text: str
    created: float
    consumed: bool = False


def _purge_expired() -> None:
    now = time.time()
    expired = [k for k, d in _DRAFTS.items() if now - d.created > DRAFT_TTL_SEC]
    for k in expired:
        _DRAFTS.pop(k, None)


def create_send_draft(recipient: str, text: str, service: str = "iMessage") -> dict[str, Any]:
    """Stage a send. Returns {draft_id, recipient, service, preview, expires_at}."""
    _purge_expired()
    if not recipient or not text:
        return {"error": "recipient and text are required"}
    if service not in ("iMessage", "SMS"):
        return {"error": f"unsupported service: {service}"}
    draft_id = secrets.token_urlsafe(12)
    _DRAFTS[draft_id] = Draft(
        draft_id=draft_id,
        recipient=recipient,
        service=service,
        text=text,
        created=time.time(),
    )
    return {
        "draft_id": draft_id,
        "recipient": recipient,
        "service": service,
        "preview": text[:200],
        "expires_at": time.time() + DRAFT_TTL_SEC,
    }


def confirm_draft(draft_id: str) -> dict[str, Any]:
    """Commit a previously-staged draft."""
    _purge_expired()
    draft = _DRAFTS.get(draft_id)
    if not draft:
        return {"sent": False, "error": "draft not found or expired"}
    if draft.consumed:
        return {"sent": False, "error": "draft already consumed"}
    draft.consumed = True

    try:
        _send_via_applescript(draft.recipient, draft.text, draft.service)
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "sent": True,
        "recipient": draft.recipient,
        "service": draft.service,
        "ts": time.time(),
    }


def _as_string(value: str) -> str:
    """Render a Python string as an AppleScript string expression.

    json.dumps is close to AppleScript's literal syntax but wrong in two ways
    that both fail as a COMPILE error, not a send error, so the message never
    leaves and the caller sees "syntax error ... found unknown token (-2741)":

      - ensure_ascii=True escapes non-ASCII to \\uXXXX, and AppleScript has no
        \\u escape at all. One emoji anywhere in the text kills the whole send.
      - AppleScript string literals cannot contain a line break, and json.dumps
        renders one as \\n, which it also cannot read.

    So: keep the characters literal, and splice real line breaks in as the
    `linefeed` constant rather than trying to escape them.
    """
    parts = value.split("\n")
    literals = [json.dumps(p, ensure_ascii=False) for p in parts]
    return " & linefeed & ".join(literals)


def _is_chat_guid(recipient: str) -> bool:
    """True for a group/chat guid ("any;+;<room>", "iMessage;-;<handle>")."""
    return ";+;" in recipient or ";-;" in recipient


def _send_via_applescript(recipient: str, text: str, service: str) -> None:
    """Send a message through Messages.app via osascript."""
    as_text = _as_string(text)

    if _is_chat_guid(recipient):
        # A group is a `chat`, never a `buddy`, so the buddy lookup below can
        # never reach one. Messages.app reports the chat id in the same form
        # chat.db and list_chats use ("any;+;<room>", verified 2026-08-29), so
        # match the guid exactly as given, and fall back to the room portion
        # alone in case a caller passes a service-qualified variant.
        room = recipient.split(";")[-1]
        as_exact = _as_string(recipient)
        as_room = _as_string(room)
        script = f"""
    on run
        set targetText to {as_text}
        tell application "Messages"
            try
                set targetChat to (first chat whose id is {as_exact})
            on error
                set targetChat to (first chat whose id contains {as_room})
            end try
            send targetText to targetChat
        end tell
    end run
    """
    else:
        # Scope the buddy to its service rather than looking it up globally.
        # The old fallback, `first buddy whose id contains ...`, walks every
        # buddy on every service, and on the Mac mini that never returns: it
        # hung past the 20s timeout on every attempt, 2026-08-29, while the
        # service-scoped form below is the one that has always worked there.
        as_recipient = _as_string(recipient)
        as_service = "iMessage" if service == "iMessage" else "SMS"
        script = f"""
    on run
        set targetText to {as_text}
        set targetRecipient to {as_recipient}
        tell application "Messages"
            set targetService to (1st account whose service type = {as_service})
            try
                set targetBuddy to participant targetRecipient of targetService
            on error
                set targetBuddy to buddy targetRecipient of targetService
            end try
            send targetText to targetBuddy
        end tell
    end run
    """
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "osascript failed")


def mark_chat_read(chat_guid: str) -> dict[str, Any]:
    """Mark a chat read via AppleScript. One-step (low-consequence)."""
    if not chat_guid:
        return {"marked": False, "error": "chat_guid required"}
    js_guid = _as_string(chat_guid)
    # Messages.app doesn't expose `chat by guid` directly; the supported path
    # is via id-string match on the chat's identifier. We try id first.
    script = f"""
    tell application "Messages"
        try
            set targetChat to first chat whose id is {js_guid}
            tell targetChat to set unread count to 0
            return "ok"
        on error errMsg
            return "err:" & errMsg
        end try
    end tell
    """
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or out.startswith("err:"):
        return {
            "marked": False,
            "error": out or proc.stderr.strip() or "osascript failed",
        }
    return {"marked": True}
