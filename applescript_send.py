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


def _send_via_applescript(recipient: str, text: str, service: str) -> None:
    """Send a message through Messages.app via osascript."""
    # JSON-encode for safe interpolation into the AppleScript literal.
    js_text = json.dumps(text)
    js_recipient = json.dumps(recipient)
    js_service = json.dumps(service)
    script = f"""
    on run
        set targetText to {js_text}
        set targetRecipient to {js_recipient}
        tell application "Messages"
            try
                set targetBuddy to buddy targetRecipient
            on error
                -- Fall back to first matching buddy by id substring across services.
                set targetBuddy to (first buddy whose id contains targetRecipient)
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
    js_guid = json.dumps(chat_guid)
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
