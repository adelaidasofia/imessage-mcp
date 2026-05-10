"""imessage-mcp — FastMCP server.

Exposes iMessage chat.db to Claude over stdio. 12 read tools + 2 write tools.
The server runs as a Claude.app subprocess; chat.db reads inherit the parent
TCC grant (Full Disk Access).

All inbound message text is run through scrubber.scrub() before returning to
Claude. CRM context auto-injection runs on tools that surface a single
contact's messages.
"""
from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

import audit
import chatdb
import contacts
import crm
import export as export_mod
import search_index
import scrubber
import applescript_send as send_mod
import transcribe

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("imessage-mcp")


@asynccontextmanager
async def lifespan(_app: FastMCP):
    """Smoke-test FDA at startup so we fail loudly instead of silently."""
    hc = chatdb.healthcheck()
    if hc["fda_granted"]:
        log.info(
            "chat.db reachable: %d chats, %d messages, %d voice notes",
            hc["chat_count"],
            hc["message_count"],
            hc["voice_note_count"],
        )
    else:
        log.warning("chat.db unreachable at startup: %s", hc.get("error"))
    yield


mcp = FastMCP("imessage-mcp", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scrub_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply scrubber.scrub to every message text. Preserves shape."""
    out = []
    for m in messages:
        scrubbed_text, flags = scrubber.scrub(m.get("text"))
        m2 = dict(m)
        m2["text"] = scrubbed_text
        if flags:
            m2["scrubbed_patterns"] = flags
        out.append(m2)
    return out


def _enrich_handle(handle: str) -> dict[str, Any]:
    """Return a small handle metadata block: real name + CRM context (if any)."""
    name = contacts.resolve_handle(handle)
    out: dict[str, Any] = {"handle": handle}
    if name:
        out["display_name"] = name
    ctx = crm.lookup_crm_context(handle=handle, display_name=name)
    if ctx:
        out["crm_context"] = ctx.to_dict()
    return out


def _audited(tool_name: str, params: dict[str, Any]):
    """Decorator-like helper: returns a context manager logging duration + summary."""

    class _A:
        def __init__(self) -> None:
            self.started = time.time()
            self.summary = ""
            self.error: str | None = None

        def __enter__(self) -> "_A":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            duration_ms = int((time.time() - self.started) * 1000)
            if exc:
                self.error = f"{exc_type.__name__}: {exc}"
            audit.log(tool_name, params, self.summary, duration_ms, self.error)
            return False

    return _A()


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
def healthcheck() -> dict[str, Any]:
    """Return FDA + chat.db reachability + Whisper status + search index status."""
    with _audited("healthcheck", {}) as a:
        out = {
            "chatdb": chatdb.healthcheck(),
            "transcription": transcribe.transcription_status(),
            "search_index": search_index.status(),
            "scrubber_enabled": scrubber.ENABLED,
            "vault_out": str(export_mod.VAULT_OUT),
            "crm_path": crm.VAULT_CRM_PATH,
            "schema_version": "1.0.0",
        }
        a.summary = f"fda={out['chatdb']['fda_granted']}"
        return out


@mcp.tool()
def list_chats(
    limit: int = 50,
    unread_only: bool = False,
    include_groups: bool = True,
    days_back: int | None = None,
) -> list[dict[str, Any]]:
    """List chats sorted by most recent activity. Adds resolved display names."""
    with _audited(
        "list_chats",
        {"limit": limit, "unread_only": unread_only, "include_groups": include_groups, "days_back": days_back},
    ) as a:
        chats = chatdb.list_chats(
            limit=limit, unread_only=unread_only, include_groups=include_groups, days_back=days_back
        )
        for c in chats:
            c["display_name_resolved"] = export_mod._resolve_chat_display(c)
        a.summary = f"returned {len(chats)} chats"
        return chats


@mcp.tool()
def list_messages(
    chat_id: int,
    limit: int = 50,
    before_rowid: int | None = None,
    include_crm_context: bool = True,
) -> dict[str, Any]:
    """Pull recent messages for one chat, scrubbed and (optionally) CRM-enriched."""
    with _audited(
        "list_messages",
        {"chat_id": chat_id, "limit": limit, "before_rowid": before_rowid},
    ) as a:
        chat = chatdb.get_chat(chat_id)
        if not chat:
            a.summary = "chat not found"
            return {"error": "chat not found", "chat_id": chat_id}
        messages = chatdb.list_messages(chat_id=chat_id, limit=limit, before_rowid=before_rowid)
        messages = _scrub_messages(messages)

        out: dict[str, Any] = {
            "chat": {
                "chat_rowid": chat["chat_rowid"],
                "chat_guid": chat["chat_guid"],
                "kind": chat["kind"],
                "display_name": export_mod._resolve_chat_display(chat),
                "service": chat["service"],
                "participants": chat["participants"],
            },
            "messages": messages,
        }
        if include_crm_context and chat["kind"] == "direct" and chat["participants"]:
            handle = chat["participants"][0]["handle"]
            out["crm_context"] = _enrich_handle(handle)
        a.summary = f"chat {chat_id}: {len(messages)} messages"
        return out


@mcp.tool()
def get_chat(chat_id: int) -> dict[str, Any] | None:
    """Return one chat's metadata + participants by ROWID or guid."""
    with _audited("get_chat", {"chat_id": chat_id}) as a:
        chat = chatdb.get_chat(chat_id)
        if not chat:
            a.summary = "not found"
            return None
        chat["display_name_resolved"] = export_mod._resolve_chat_display(chat)
        if chat["kind"] == "direct" and chat["participants"]:
            chat["crm_context"] = _enrich_handle(chat["participants"][0]["handle"])
        a.summary = f"chat {chat_id} resolved"
        return chat


@mcp.tool()
def search_contacts(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Find handles by substring; enriches with Contacts.app names + CRM context."""
    with _audited("search_contacts", {"query": query, "limit": limit}) as a:
        rows = chatdb.search_contacts_by_handle(query, limit=limit)
        for r in rows:
            r["display_name"] = contacts.resolve_handle(r["handle"])
            ctx = crm.lookup_crm_context(handle=r["handle"], display_name=r["display_name"])
            if ctx:
                r["crm_context"] = ctx.to_dict()
        a.summary = f"{len(rows)} matches"
        return rows


@mcp.tool()
def search_messages(
    query: str,
    chat_id: int | None = None,
    sender: str | None = None,
    days_back: int | None = None,
    limit: int = 50,
    refresh_index: bool = True,
) -> list[dict[str, Any]]:
    """FTS5 full-text search over messages. Refreshes the index by default."""
    with _audited(
        "search_messages",
        {"query": query, "chat_id": chat_id, "sender": sender, "days_back": days_back, "limit": limit},
    ) as a:
        if refresh_index:
            search_index.refresh_index()
        rows = search_index.search(
            query=query, chat_id=chat_id, sender=sender, days_back=days_back, limit=limit
        )
        for r in rows:
            scrubbed, flags = scrubber.scrub(r.get("text"))
            r["text"] = scrubbed
            if flags:
                r["scrubbed_patterns"] = flags
        a.summary = f"{len(rows)} hits"
        return rows


@mcp.tool()
def get_message_context(rowid: int, before: int = 5, after: int = 5) -> list[dict[str, Any]]:
    """Return messages surrounding a given ROWID in the same chat."""
    with _audited(
        "get_message_context", {"rowid": rowid, "before": before, "after": after}
    ) as a:
        msgs = chatdb.get_message_context(rowid=rowid, before=before, after=after)
        msgs = _scrub_messages(msgs)
        a.summary = f"{len(msgs)} surrounding messages"
        return msgs


@mcp.tool()
def get_unread(limit: int = 50) -> list[dict[str, Any]]:
    """Return unread inbound messages across all chats (newest first)."""
    with _audited("get_unread", {"limit": limit}) as a:
        msgs = chatdb.get_unread(limit=limit)
        msgs = _scrub_messages(msgs)
        a.summary = f"{len(msgs)} unread"
        return msgs


@mcp.tool()
def get_thread(originator_guid: str) -> list[dict[str, Any]]:
    """Return every message in a reply thread."""
    with _audited("get_thread", {"originator_guid": originator_guid}) as a:
        msgs = chatdb.get_thread(originator_guid)
        msgs = _scrub_messages(msgs)
        a.summary = f"{len(msgs)} messages in thread"
        return msgs


@mcp.tool()
def list_attachments(
    chat_id: int | None = None,
    days_back: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List attachments, filterable by chat or recency."""
    with _audited(
        "list_attachments",
        {"chat_id": chat_id, "days_back": days_back, "limit": limit},
    ) as a:
        rows = chatdb.list_attachments(chat_id=chat_id, days_back=days_back, limit=limit)
        a.summary = f"{len(rows)} attachments"
        return rows


@mcp.tool()
def mark_chat_read(chat_guid: str) -> dict[str, Any]:
    """Mark a chat read via Messages.app AppleScript (low-consequence, no draft)."""
    with _audited("mark_chat_read", {"chat_guid": chat_guid}) as a:
        result = send_mod.mark_chat_read(chat_guid)
        a.summary = f"marked={result.get('marked', False)}"
        return result


@mcp.tool()
def export_to_vault(
    min_messages: int = 1,
    include_groups: bool = True,
    contact_filter: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Walk every chat meeting the threshold and write per-chat Markdown to the vault.

    Idempotent: skips chats whose vault frontmatter `last_message_ts` already
    covers the latest message in chat.db. Pass force=True to rewrite all.
    """
    with _audited(
        "export_to_vault",
        {
            "min_messages": min_messages,
            "include_groups": include_groups,
            "contact_filter": contact_filter,
            "force": force,
        },
    ) as a:
        result = export_mod.export_to_vault(
            min_messages=min_messages,
            include_groups=include_groups,
            contact_filter=contact_filter,
            force=force,
        )
        a.summary = (
            f"wrote {result['chats_written']}, skipped {result['chats_skipped_unchanged']} "
            f"unchanged, transcribed {result['voice_notes_transcribed']} voice notes"
        )
        return result


# ---------------------------------------------------------------------------
# Write tools (draft + confirm pattern, mirrors whatsapp-mcp)
# ---------------------------------------------------------------------------


@mcp.tool()
def send_message(
    recipient: str, text: str, service: str = "iMessage"
) -> dict[str, Any]:
    """Stage an iMessage (or SMS) send. Returns a draft_id; nothing is sent yet.

    Args:
        recipient: phone (E.164 with leading +) or email handle
        text: message body
        service: "iMessage" or "SMS" (SMS requires Continuity to your iPhone)
    """
    with _audited(
        "send_message",
        {"recipient": recipient, "service": service, "len": len(text or "")},
    ) as a:
        result = send_mod.create_send_draft(recipient=recipient, text=text, service=service)
        a.summary = f"drafted {result.get('draft_id', '?')}"
        return result


@mcp.tool()
def confirm_send(draft_id: str) -> dict[str, Any]:
    """Commit a previously-drafted send. Runs the AppleScript and returns success."""
    with _audited("confirm_send", {"draft_id": draft_id}) as a:
        result = send_mod.confirm_draft(draft_id)
        a.summary = f"sent={result.get('sent', False)}"
        return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    mcp.run()
