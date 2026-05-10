"""export.py — vault Markdown writer for iMessage chats.

Output layout (mirrors whatsapp-mcp shape so graphify, Dataview, and the
crm_context block already work without changes):

  <IMESSAGE_VAULT_OUT>/<Contact Name>.md          (1:1 direct chat)
  <IMESSAGE_VAULT_OUT>/Groups/<Group Name>.md     (group chat)
  <IMESSAGE_VAULT_OUT>/Attachments/<guid>-<filename>   (copied attachments)

YAML frontmatter:
  type: imessage-chat
  contact: "<display>"
  service: iMessage|SMS|Mixed
  chat_guid: "<guid>"
  participants: [...]   (groups only)
  message_count: <N>
  first_message: YYYY-MM-DD
  last_message: YYYY-MM-DD
  last_message_ts: <unix>
  last_sync: YYYY-MM-DD

Body: per-day `## YYYY-MM-DD` sections, then `**HH:MM AM** Speaker: <text>`
lines. Reactions render as `↳ ❤️ Alice reacted to: "..."`. Edits surface as
`[edited at HH:MM]`. Replies render the quoted message above the response.
Voice notes inline as `[Voice note] <transcript>`. Image attachments render
as `![[Attachments/<filename>]]` (Obsidian inline preview).
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import frontmatter

import chatdb
import contacts
import transcribe

VAULT_OUT = Path(
    os.environ.get(
        "IMESSAGE_VAULT_OUT",
        str(
            Path.home()
            / "Documents"
            / "Vault"
            / "🤖 AI Chats"
            / "iMessage"
        ),
    )
)
ATTACHMENTS_ROOT_SRC = Path.home() / "Library" / "Messages" / "Attachments"

# Frontmatter keys this exporter owns. Anything else found in an existing
# chat file is treated as extractor / user-added data and round-tripped on
# overwrite. Without this, every export nukes vault-metadata-extract's
# imessage_* fields (and any other downstream-added keys like word_count).
# Includes both singular (single-alias) and plural (alias-merged) variants.
_BRIDGE_CANONICAL_KEYS = frozenset({
    "type", "contact", "service",
    "chat_guid", "chat_guids",
    "chat_rowid", "chat_rowids",
    "kind", "message_count",
    "first_message", "last_message", "last_message_ts", "last_sync",
    "email", "emails", "phone", "phones",
    "participants",
})


def _with_preserved(meta: dict[str, Any], target_path: Path) -> dict[str, Any]:
    """Merge meta with non-canonical keys from the existing file (if any).

    Canonical keys (the ones this exporter writes) are always overwritten
    with the fresh values in `meta`. Everything else found in the existing
    file's frontmatter is preserved verbatim.
    """
    if not target_path.exists():
        return meta
    try:
        existing = frontmatter.load(str(target_path))
        preserved = {
            k: v
            for k, v in (existing.metadata or {}).items()
            if k not in _BRIDGE_CANONICAL_KEYS
        }
    except Exception:
        return meta
    return {**preserved, **meta}


# associated_message_type tapback decoding
_TAPBACK_EMOJI = {
    2000: "❤️",
    2001: "👍",
    2002: "👎",
    2003: "😂",
    2004: "‼️",
    2005: "❓",
    2006: "📷",  # sticker placeholder
    2007: "✨",  # custom emoji placeholder
}
_TAPBACK_REMOVED = {3000, 3001, 3002, 3003, 3004, 3005, 3006, 3007}

# Filename safety
_UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


@dataclass
class ExportSummary:
    chats_seen: int = 0
    chats_written: int = 0
    chats_skipped_empty: int = 0
    chats_skipped_unchanged: int = 0
    voice_notes_transcribed: int = 0
    attachments_copied: int = 0
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "chats_seen": self.chats_seen,
            "chats_written": self.chats_written,
            "chats_skipped_empty": self.chats_skipped_empty,
            "chats_skipped_unchanged": self.chats_skipped_unchanged,
            "voice_notes_transcribed": self.voice_notes_transcribed,
            "attachments_copied": self.attachments_copied,
            "duration_ms": self.duration_ms,
            "vault_out": str(VAULT_OUT),
        }


def export_to_vault(
    min_messages: int = 1,
    include_groups: bool = True,
    contact_filter: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Walk every chat that meets the threshold and write Markdown.

    Args:
        min_messages: skip chats with fewer than N messages
        include_groups: include style=43 group chats
        contact_filter: substring against display name or handle (case-insensitive)
        force: ignore last_message_ts mtime check; rewrite all chats
    """
    started = time.time()
    summary = ExportSummary()

    VAULT_OUT.mkdir(parents=True, exist_ok=True)
    (VAULT_OUT / "Groups").mkdir(parents=True, exist_ok=True)
    (VAULT_OUT / "Attachments").mkdir(parents=True, exist_ok=True)

    chats = chatdb.list_chats(
        limit=10_000, unread_only=False, include_groups=include_groups
    )
    summary.chats_seen = len(chats)

    contact_filter_lower = (contact_filter or "").strip().lower()

    # Bucket direct chats by resolved display name so the same person across
    # phone + email threads merges into one file. Groups stay one-file-per-chat.
    direct_buckets: dict[str, list[dict[str, Any]]] = {}
    group_chats: list[dict[str, Any]] = []
    for chat in chats:
        if chat["kind"] == "group":
            group_chats.append(chat)
        else:
            display = _resolve_chat_display(chat)
            direct_buckets.setdefault(display, []).append(chat)

    # Direct chats — merged per name.
    for display, chat_list in direct_buckets.items():
        total_msgs = sum(c["message_count"] for c in chat_list)
        if total_msgs < min_messages:
            summary.chats_skipped_empty += len(chat_list)
            continue

        if contact_filter_lower:
            blob = " ".join(
                [display.lower()]
                + [c.get("chat_identifier", "").lower() for c in chat_list]
                + [
                    p["handle"].lower()
                    for c in chat_list
                    for p in c.get("participants", [])
                ]
            )
            if contact_filter_lower not in blob:
                continue

        target_path = VAULT_OUT / f"{_safe_filename(display)}.md"
        latest_ts = max(c["last_message_ts"] for c in chat_list)
        if not force and _is_unchanged(target_path, latest_ts):
            summary.chats_skipped_unchanged += 1
            continue

        try:
            if _write_merged_direct(chat_list, display, target_path, summary):
                summary.chats_written += 1
        except Exception as e:  # noqa: BLE001
            _log_error(target_path, e)

    # Groups — one file per chat.
    for chat in group_chats:
        if chat["message_count"] < min_messages:
            summary.chats_skipped_empty += 1
            continue

        display = _resolve_chat_display(chat)
        if contact_filter_lower:
            blob = " ".join(
                [
                    display.lower(),
                    chat.get("chat_identifier", "").lower(),
                    " ".join(p["handle"].lower() for p in chat.get("participants", [])),
                ]
            )
            if contact_filter_lower not in blob:
                continue

        target_path = _target_path_for(chat, display)
        if not force and _is_unchanged(target_path, chat["last_message_ts"]):
            summary.chats_skipped_unchanged += 1
            continue

        try:
            if _write_chat_file(chat, display, target_path, summary):
                summary.chats_written += 1
        except Exception as e:  # noqa: BLE001
            _log_error(target_path, e)

    summary.duration_ms = int((time.time() - started) * 1000)
    return summary.as_dict()


def _write_merged_direct(
    chat_list: list[dict[str, Any]],
    display: str,
    target_path: Path,
    summary: ExportSummary,
) -> bool:
    """Merge multiple direct chats with the same resolved person into one file.

    Pulls every message across the chats, dedupes by guid, orders by timestamp,
    then runs the same render pipeline as a single chat. Frontmatter lists all
    underlying chat_guids and aggregates handles.
    """
    by_guid: dict[str, dict[str, Any]] = {}
    tapbacks_by_target: dict[str, list[dict[str, Any]]] = {}
    rendered_messages: list[dict[str, Any]] = []
    handles: list[str] = []
    services: set[str] = set()

    for chat in chat_list:
        for p in chat.get("participants", []):
            if p["handle"] not in handles:
                handles.append(p["handle"])

        msgs = chatdb.list_messages_chronological(chat["chat_rowid"])
        for m in msgs:
            if m["service"]:
                services.add(m["service"])
            if m["guid"] in by_guid:
                continue
            by_guid[m["guid"]] = m
            if 2000 <= m["associated_message_type"] <= 2007:
                target = m["associated_message_guid"] or ""
                target = target.split(":", 1)[-1] if ":" in target else target
                tapbacks_by_target.setdefault(target, []).append(m)
                continue
            if m["associated_message_type"] in _TAPBACK_REMOVED:
                continue
            if m["is_system_message"] or m["item_type"] != 0:
                continue
            rendered_messages.append(m)

    if not rendered_messages:
        summary.chats_skipped_empty += len(chat_list)
        return False

    rendered_messages.sort(key=lambda m: (m["ts"], m["rowid"]))

    # Pull attachments per message in one go.
    with chatdb.open_db() as conn:
        atts_by_msg: dict[int, list[dict[str, Any]]] = {}
        for m in rendered_messages:
            if m["has_attachments"]:
                atts_by_msg[m["rowid"]] = chatdb.attachments_for_message(conn, m["rowid"])

    first_ts = rendered_messages[0]["ts"]
    last_ts = rendered_messages[-1]["ts"]
    service_value = "Mixed" if len(services) > 1 else (next(iter(services), "iMessage"))

    meta: dict[str, Any] = {
        "type": "imessage-chat",
        "contact": display,
        "service": service_value,
        "chat_guids": [c["chat_guid"] for c in chat_list],
        "chat_rowids": [c["chat_rowid"] for c in chat_list],
        "kind": "direct",
        "message_count": len(rendered_messages),
        "first_message": _fmt_date(first_ts),
        "last_message": _fmt_date(last_ts),
        "last_message_ts": last_ts,
        "last_sync": _fmt_date(int(time.time())),
    }
    phones = [h for h in handles if "@" not in h]
    emails = [h.lower() for h in handles if "@" in h]
    if phones:
        meta["phones"] = phones if len(phones) > 1 else phones[0]
    if emails:
        meta["emails"] = emails if len(emails) > 1 else emails[0]

    body_lines: list[str] = []
    last_date = ""
    for m in rendered_messages:
        d = _fmt_date(m["ts"])
        if d != last_date:
            body_lines.append(f"\n## {d}\n")
            last_date = d
        body_lines.append(
            _render_message(m, by_guid, tapbacks_by_target, atts_by_msg, summary, target_path)
        )

    post = frontmatter.Post(
        "\n".join(body_lines).strip() + "\n",
        **_with_preserved(meta, target_path),
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(frontmatter.dumps(post).encode("utf-8"))
    return True


# ---------------------------------------------------------------------------
# Naming + paths
# ---------------------------------------------------------------------------


def _resolve_chat_display(chat: dict[str, Any]) -> str:
    """Pick the human-readable name for the chat."""
    if chat["kind"] == "group":
        if chat.get("display_name"):
            return chat["display_name"]
        # Build from participants when no group name is set.
        names = []
        for p in chat.get("participants", []):
            n = contacts.resolve_handle(p["handle"]) or p["handle"]
            names.append(n)
        if names:
            return ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
        return chat.get("chat_identifier", "Group Chat")

    # Direct: prefer Contacts.app name → display_name → chat_identifier → first participant handle
    handle = ""
    parts = chat.get("participants", [])
    if parts:
        handle = parts[0]["handle"]
    elif chat.get("chat_identifier"):
        handle = chat["chat_identifier"]
    name = contacts.resolve_handle(handle) if handle else None
    if name:
        return name
    if chat.get("display_name"):
        return chat["display_name"]
    return handle or chat.get("chat_identifier", "Unknown")


def _target_path_for(chat: dict[str, Any], display: str) -> Path:
    safe = _safe_filename(display)
    if chat["kind"] == "group":
        return VAULT_OUT / "Groups" / f"{safe}.md"
    return VAULT_OUT / f"{safe}.md"


def _safe_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip()
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    if not cleaned:
        cleaned = "Unknown"
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip()
    return cleaned


def _is_unchanged(path: Path, last_message_ts: int) -> bool:
    """Return True if the existing vault file already covers up to last_message_ts."""
    if not path.exists():
        return False
    try:
        post = frontmatter.load(str(path))
        existing = int(post.metadata.get("last_message_ts", 0) or 0)
    except Exception:
        return False
    return existing >= last_message_ts and last_message_ts > 0


def _log_error(path: Path, err: Exception) -> None:
    log_path = Path.home() / ".claude" / "imessage-mcp" / "export-errors.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {path}: "
                f"{type(err).__name__}: {err}\n"
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-chat write
# ---------------------------------------------------------------------------


def _write_chat_file(
    chat: dict[str, Any],
    display: str,
    target_path: Path,
    summary: ExportSummary,
) -> bool:
    messages = chatdb.list_messages_chronological(chat["chat_rowid"])
    if not messages:
        summary.chats_skipped_empty += 1
        return False

    # Group tapbacks by their target message guid.
    tapbacks_by_target: dict[str, list[dict[str, Any]]] = {}
    rendered_messages: list[dict[str, Any]] = []
    by_guid: dict[str, dict[str, Any]] = {}

    for m in messages:
        by_guid[m["guid"]] = m
        if 2000 <= m["associated_message_type"] <= 2007:
            target = m["associated_message_guid"] or ""
            # Strip the bp:/p: prefix that Apple uses on tapback targets.
            target = target.split(":", 1)[-1] if ":" in target else target
            tapbacks_by_target.setdefault(target, []).append(m)
            continue
        if m["associated_message_type"] in _TAPBACK_REMOVED:
            continue
        if m["is_system_message"] or m["item_type"] != 0:
            # Skip "X named the conversation", "X joined", etc.
            continue
        rendered_messages.append(m)

    # Pull attachments per message.
    with chatdb.open_db() as conn:
        atts_by_msg: dict[int, list[dict[str, Any]]] = {}
        for m in rendered_messages:
            if m["has_attachments"]:
                atts_by_msg[m["rowid"]] = chatdb.attachments_for_message(conn, m["rowid"])

    # Build YAML frontmatter.
    services = {m["service"] for m in rendered_messages if m["service"]}
    service_value = "Mixed" if len(services) > 1 else (next(iter(services), chat["service"]))

    handle = ""
    if chat["kind"] == "direct" and chat.get("participants"):
        handle = chat["participants"][0]["handle"]

    first_ts = rendered_messages[0]["ts"] if rendered_messages else 0
    last_ts = rendered_messages[-1]["ts"] if rendered_messages else chat["last_message_ts"]

    meta: dict[str, Any] = {
        "type": "imessage-chat",
        "contact": display,
        "service": service_value,
        "chat_guid": chat["chat_guid"],
        "chat_rowid": chat["chat_rowid"],
        "kind": chat["kind"],
        "message_count": len(rendered_messages),
        "first_message": _fmt_date(first_ts) if first_ts else "",
        "last_message": _fmt_date(last_ts) if last_ts else "",
        "last_message_ts": last_ts,
        "last_sync": _fmt_date(int(time.time())),
    }
    if chat["kind"] == "direct" and handle:
        if "@" in handle:
            meta["email"] = handle.lower()
        else:
            meta["phone"] = handle
    if chat["kind"] == "group":
        meta["participants"] = [p["handle"] for p in chat.get("participants", [])]

    # Render body.
    body_lines: list[str] = []
    last_date = ""
    for m in rendered_messages:
        d = _fmt_date(m["ts"])
        if d != last_date:
            body_lines.append(f"\n## {d}\n")
            last_date = d
        body_lines.append(_render_message(m, by_guid, tapbacks_by_target, atts_by_msg, summary, target_path))

    post = frontmatter.Post(
        "\n".join(body_lines).strip() + "\n",
        **_with_preserved(meta, target_path),
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(frontmatter.dumps(post).encode("utf-8"))
    return True


def _render_message(
    m: dict[str, Any],
    by_guid: dict[str, dict[str, Any]],
    tapbacks_by_target: dict[str, list[dict[str, Any]]],
    atts_by_msg: dict[int, list[dict[str, Any]]],
    summary: ExportSummary,
    target_path: Path,
) -> str:
    speaker = _speaker_for(m)
    timestamp = _fmt_clock(m["ts"])
    out: list[str] = []

    # Reply quote (only for user-visible inline replies — thread_originator_guid).
    # `reply_to_guid` is set on most messages for delivery threading and is NOT
    # the user-quoted-reply marker. Verified 2026-05-07: 9,114 rows have
    # reply_to_guid set vs 539 with thread_originator_guid (the actual reply UI).
    if m["thread_originator_guid"] and m["thread_originator_guid"] != m["guid"]:
        target = by_guid.get(m["thread_originator_guid"])
        if target and target.get("text"):
            preview = _truncate(target["text"], 200)
            out.append(f"> {_speaker_for(target)}: {preview}")

    text = m["text"] or ""
    suffixes: list[str] = []
    if m["date_edited_ts"]:
        suffixes.append(f"[edited at {_fmt_clock(m['date_edited_ts'])}]")
    if m["is_from_me"] and m["date_read_ts"]:
        suffixes.append(f"[seen at {_fmt_clock(m['date_read_ts'])}]")

    # Attachments
    attachments = atts_by_msg.get(m["rowid"], [])
    attachment_lines: list[str] = []
    if attachments:
        for att in attachments:
            line = _render_attachment(m, att, summary, target_path)
            if line:
                attachment_lines.append(line)

    # Compose message line
    suffix = " ".join(suffixes)
    body = text.strip()
    if attachment_lines and not body:
        body = " ".join(attachment_lines)
        attachment_lines = []
    line = f"**{timestamp}** {speaker}: {body}".rstrip()
    if suffix:
        line = f"{line} {suffix}"
    out.append(line)
    out.extend(f"  {al}" for al in attachment_lines)

    # Reactions targeting this message
    target_keys = [m["guid"], f"p:0/{m['guid']}", f"bp:{m['guid']}"]
    seen_reactions: list[dict[str, Any]] = []
    for k in target_keys:
        seen_reactions.extend(tapbacks_by_target.get(k, []))
        # Also try stripping the prefix from stored keys
    # The targets stored in tapbacks_by_target already had the prefix stripped,
    # so plain guid is the canonical lookup.
    seen_reactions = tapbacks_by_target.get(m["guid"], []) or seen_reactions

    for r in sorted(seen_reactions, key=lambda x: x["ts"]):
        emoji = r["associated_message_emoji"] or _TAPBACK_EMOJI.get(
            r["associated_message_type"], "⭐"
        )
        out.append(f"  ↳ {emoji} {_speaker_for(r)} reacted")

    return "\n".join(out)


def _speaker_for(m: dict[str, Any]) -> str:
    if m["is_from_me"]:
        return "You"
    handle = m.get("sender_handle", "")
    name = contacts.resolve_handle(handle) if handle else None
    return name or (handle or "Unknown")


def _render_attachment(
    m: dict[str, Any],
    att: dict[str, Any],
    summary: ExportSummary,
    target_path: Path,
) -> str:
    """Resolve, optionally copy, and render a single attachment."""
    src_filename = att.get("filename") or ""
    if not src_filename:
        return f"[file: {att.get('transfer_name', 'unknown')}]"
    src_path = Path(os.path.expanduser(src_filename))
    if not src_path.exists():
        return f"[attachment expired: {src_path.name}]"

    mime = att.get("mime_type", "") or ""
    uti = att.get("uti", "") or ""

    # Voice note
    is_audio = (
        m["is_audio_message"]
        or src_path.suffix.lower() in (".caf", ".m4a", ".aac", ".mp3", ".wav")
        or mime.startswith("audio/")
        or "audio" in uti
    )
    if is_audio:
        transcript = transcribe.transcribe_audio(src_path, attachment_guid=att["guid"])
        if transcript:
            summary.voice_notes_transcribed += 1
            return f"[Voice note] {transcript}"
        return "[Voice note]"

    # Image / file: copy into vault
    safe_name = _safe_filename(src_path.name)
    dest_name = f"{att['guid'][:8]}-{safe_name}"
    dest = VAULT_OUT / "Attachments" / dest_name
    if not dest.exists():
        try:
            shutil.copy2(src_path, dest)
            summary.attachments_copied += 1
        except Exception:
            return f"[attachment unreadable: {src_path.name}]"

    is_image = mime.startswith("image/") or src_path.suffix.lower() in (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".heic",
        ".webp",
    )
    if is_image:
        return f"![[Attachments/{dest_name}]]"
    return f"[file: [[Attachments/{dest_name}]]]"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _fmt_date(unix_ts: int) -> str:
    if not unix_ts:
        return ""
    return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d")


def _fmt_clock(unix_ts: int) -> str:
    if not unix_ts:
        return ""
    return datetime.fromtimestamp(unix_ts).strftime("%I:%M %p")


def _truncate(s: str, max_len: int) -> str:
    s = s.strip().replace("\n", " ")
    return s if len(s) <= max_len else s[: max_len - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import json
    import sys

    min_msg = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(json.dumps(export_to_vault(min_messages=min_msg), indent=2))
