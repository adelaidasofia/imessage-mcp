"""export.py — vault Markdown writer for iMessage chats.

Output layout (mirrors whatsapp-mcp shape so graphify, Dataview, and the
crm_context block already work without changes):

  <IMESSAGE_VAULT_OUT>/<Contact Name>.md          (1:1 direct chat)
  <IMESSAGE_VAULT_OUT>/Groups/<Group Name>.md     (group chat)
  <IMESSAGE_VAULT_OUT>/Attachments/<guid>-<filename>   (copied attachments)

When no name is known, a chat is never filed under its phone number, email or
chat identifier (tickets#6939): Studio shows the file name as the title. It is
filed as "Chat with an unknown number (iMessage) <id>.md" (or "... unknown
email ...") and "Groups/Group chat (iMessage) <id>.md", where <id> is the start
of a hash of the handle or chat guid: unique per chat and the same on every
run. The handle stays in the frontmatter (phone/email, chat_guid). An existing
file is found by its frontmatter chat_guid(s), so when a name becomes known
(or an old file sits under a bare number) it is renamed, not duplicated.

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

import hashlib
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
    # phone + email threads merges into one file. A chat with no known name
    # is bucketed by its normalised handle, so two spellings of one address
    # (capitals in an email) are one chat's file, not two chats in one file.
    # Groups stay one-file-per-chat.
    direct_buckets: dict[str, list[dict[str, Any]]] = {}
    group_chats: list[dict[str, Any]] = []
    for chat in chats:
        if chat["kind"] == "group":
            group_chats.append(chat)
        else:
            display = _resolve_chat_display(chat)
            if _is_identifier(display, [chat]):
                key = _unnamed_key_direct(display)
            else:
                key = "name:" + display
            direct_buckets.setdefault(key, []).append(chat)

    direct_stems = _plan_direct_stems(direct_buckets)
    group_stems = _plan_group_stems(group_chats)
    existing_by_guid = _index_existing_files()

    # Direct chats — merged per name.
    for key, chat_list in direct_buckets.items():
        display = min(_resolve_chat_display(c) for c in chat_list)
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

        guids = {c["chat_guid"] for c in chat_list}
        target_path = VAULT_OUT / f"{direct_stems[key]}.md"
        owned = _owned_target(target_path, guids, key)
        if owned is None:
            _log_error(target_path, FileExistsError("name taken by another file"))
            continue
        target_path = owned
        healed = _heal_existing(target_path, guids, existing_by_guid)
        latest_ts = max(c["last_message_ts"] for c in chat_list)
        if not force and not healed and _is_unchanged(target_path, latest_ts, guids):
            summary.chats_skipped_unchanged += 1
            continue

        counts: dict[str, int] = {}
        try:
            if _write_merged_direct(chat_list, display, target_path, summary, counts):
                summary.chats_written += 1
                _retire_absorbed(target_path, guids, counts, existing_by_guid)
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

        target_path = VAULT_OUT / "Groups" / f"{group_stems[chat['chat_guid']]}.md"
        guids = {chat["chat_guid"]}
        owned = _owned_target(target_path, guids, chat["chat_guid"])
        if owned is None:
            _log_error(target_path, FileExistsError("name taken by another file"))
            continue
        target_path = owned
        healed = _heal_existing(target_path, guids, existing_by_guid)
        if not force and not healed and _is_unchanged(target_path, chat["last_message_ts"], guids):
            summary.chats_skipped_unchanged += 1
            continue

        counts = {}
        try:
            if _write_chat_file(chat, display, target_path, summary, counts):
                summary.chats_written += 1
                _retire_absorbed(target_path, guids, counts, existing_by_guid)
        except Exception as e:  # noqa: BLE001
            _log_error(target_path, e)

    summary.duration_ms = int((time.time() - started) * 1000)
    return summary.as_dict()


def _write_merged_direct(
    chat_list: list[dict[str, Any]],
    display: str,
    target_path: Path,
    summary: ExportSummary,
    counts: dict[str, int] | None = None,
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
            if counts is not None:
                counts[chat["chat_guid"]] = counts.get(chat["chat_guid"], 0) + 1

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


_IDENTIFIER_RE = re.compile(r"^[\d+\s\-().]+$")


def _looks_like_identifier(name: str) -> bool:
    """A phone number, short code, email or chat id posing as a name."""
    n = (name or "").strip()
    if not n:
        return True
    if "@" in n:
        return True
    if re.fullmatch(r"chat\d+", n):
        return True
    return bool(_IDENTIFIER_RE.match(n)) and any(ch.isdigit() for ch in n)


def _is_identifier(display: str, chat_list: list[dict[str, Any]]) -> bool:
    """True when a direct chat's display is its handle, not a person's name."""
    if _looks_like_identifier(display):
        return True
    raw = {c.get("chat_identifier", "") for c in chat_list}
    raw |= {p["handle"] for c in chat_list for p in c.get("participants", [])}
    return display in raw


def _hash_id(key: str, n: int) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:n]


def _unnamed_key_direct(handle: str) -> str:
    return "direct:" + handle.strip().lower()


def _unnamed_key_group(chat: dict[str, Any]) -> str:
    return "group:" + (chat.get("chat_guid") or chat.get("chat_identifier") or "")


def _unnamed_label_direct(handle: str) -> str:
    if "@" in handle:
        return "Chat with an unknown email address (iMessage)"
    return "Chat with an unknown number (iMessage)"


def _group_title(chat: dict[str, Any]) -> tuple[str, bool] | None:
    """Readable name for a group's file, or None when no name is known.

    Returns (title, needs_id). The group's own name wins. Otherwise the
    participants whose names are in Contacts, in the same shape as before
    when every participant has a name. Handles without a name are counted,
    never printed; such a title is not unique ("Ana + 2 more" can be several
    groups), so needs_id asks the caller to add the chat's short id.
    """
    name = (chat.get("display_name") or "").strip()
    if name and not _looks_like_identifier(name):
        return name, False
    parts = chat.get("participants", [])
    names: list[str] = []
    unknown = 0
    for p in parts:
        n = contacts.resolve_handle(p["handle"])
        if n and not _looks_like_identifier(n):
            names.append(n)
        else:
            unknown += 1
    if not names:
        return None
    if not unknown:
        return ", ".join(names[:4]) + ("…" if len(parts) > 4 else ""), False
    shown = list(dict.fromkeys(names))[:4]
    return f"{', '.join(shown)} + {len(parts) - len(shown)} more", True


def _ids_for(keys: list[str]) -> dict[str, str]:
    """Short id per key: 6 hex, or 16 for every key whose 6 clash."""
    short = {k: _hash_id(k, 6) for k in keys}
    seen: dict[str, int] = {}
    for v in short.values():
        seen[v] = seen.get(v, 0) + 1
    return {k: (_hash_id(k, 16) if seen[v] > 1 else v) for k, v in short.items()}


def _unique_stems(base: dict[str, str], ids: dict[str, str]) -> dict[str, str]:
    """Add the id to every stem another chat shares (case-insensitive, as
    APFS compares names), so no two chats ever write one file."""
    folded: dict[str, int] = {}
    for stem in base.values():
        folded[stem.casefold()] = folded.get(stem.casefold(), 0) + 1
    return {
        k: (f"{stem} {ids[k]}" if folded[stem.casefold()] > 1 else stem)
        for k, stem in base.items()
    }


def _plan_direct_stems(buckets: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """File stem per direct bucket. Ids are computed among direct chats only,
    so whether groups are exported never changes a direct chat's name."""
    ids = _ids_for(sorted(buckets))
    base: dict[str, str] = {}
    for key, chat_list in buckets.items():
        if key.startswith("direct:"):
            base[key] = f"{_unnamed_label_direct(key)} {ids[key]}"
        else:
            base[key] = _safe_filename(key[len("name:"):])
    return _unique_stems(base, ids)


def _plan_group_stems(group_chats: list[dict[str, Any]]) -> dict[str, str]:
    """File stem per group chat guid; ids computed among groups only."""
    keys = {c["chat_guid"]: _unnamed_key_group(c) for c in group_chats}
    by_key = _ids_for(sorted(set(keys.values())))
    ids = {g: by_key[k] for g, k in keys.items()}
    base: dict[str, str] = {}
    for chat in group_chats:
        g = chat["chat_guid"]
        titled = _group_title(chat)
        if titled is None:
            base[g] = f"Group chat (iMessage) {ids[g]}"
        elif titled[1]:
            base[g] = f"{_safe_filename(titled[0])} {ids[g]}"
        else:
            base[g] = _safe_filename(titled[0])
    return _unique_stems(base, ids)


def _same_file(a: Path, b: Path) -> bool:
    """True when two paths open one file. On APFS (case-insensitive) names
    differing only in capitals are one file, so string equality is not
    enough."""
    try:
        return a.samefile(b)
    except OSError:
        return False


def _owned_target(
    target_path: Path, guids: set[str], key: str
) -> Path | None:
    """target_path, or a name with the chat's id when the file already there
    belongs to another chat or is not an exporter file (a user's note, or a
    chat filtered out of this run whose name matches ignoring capitals).
    None when even that name is taken."""
    for candidate in (target_path, target_path.with_name(f"{target_path.stem} {_hash_id(key, 16)}.md")):
        if not candidate.exists():
            return candidate
        owner = _file_guids(candidate)
        if owner and owner <= guids:
            return candidate
    return None


def _retire_absorbed(
    target_path: Path,
    guids: set[str],
    counts: dict[str, int],
    index: dict[str, list[tuple[Path, frozenset[str]]]],
) -> None:
    """Remove other files whose chats this file now holds.

    When a chat's name becomes known and the person already has a file, the
    chat is written into that file and its old file is left over. A leftover
    is removed only when all its chats are in this file and this write
    rendered at least as many of their messages as it recorded, so no
    message is lost; otherwise it stays. message_count only says how many
    messages the file held, not which: if the database has aged out old
    messages and gained as many new ones, the check passes and the aged-out
    ones are lost. It guards the common case, not that one.
    """
    for g in sorted(guids):
        for path, file_guids in index.get(g, []):
            if not path.exists() or not file_guids <= guids:
                continue
            if path == target_path or _same_file(path, target_path):
                continue  # the file just written, perhaps under other capitals
            try:
                old = int(frontmatter.load(str(path)).metadata.get("message_count") or 0)
            except Exception:
                continue
            if sum(counts.get(x, 0) for x in file_guids) < old:
                continue
            try:
                path.unlink()
            except OSError as e:
                _log_error(path, e)


def _file_guids(path: Path) -> set[str]:
    """chat_guid(s) an existing exporter-owned chat file declares."""
    try:
        meta = frontmatter.load(str(path)).metadata or {}
    except Exception:
        return set()
    if meta.get("type") != "imessage-chat":
        return set()
    guids: set[str] = set()
    one = meta.get("chat_guid")
    if isinstance(one, str) and one:
        guids.add(one)
    many = meta.get("chat_guids")
    if isinstance(many, str) and many:
        guids.add(many)
    elif isinstance(many, list):
        guids |= {g for g in many if isinstance(g, str) and g}
    return guids


def _index_existing_files() -> dict[str, list[tuple[Path, frozenset[str]]]]:
    """guid -> chat files that claim it, across the direct and Groups folders."""
    index: dict[str, list[tuple[Path, frozenset[str]]]] = {}
    for folder in (VAULT_OUT, VAULT_OUT / "Groups"):
        if not folder.is_dir():
            continue
        for path in folder.glob("*.md"):
            guids = frozenset(_file_guids(path))
            for g in guids:
                index.setdefault(g, []).append((path, guids))
    return index


def _heal_existing(
    target_path: Path,
    guids: set[str],
    index: dict[str, list[tuple[Path, frozenset[str]]]],
) -> bool:
    """Move this chat's existing file to target_path when it sits elsewhere.

    Files are matched by the chat_guid(s) in their frontmatter, not by name,
    so a chat whose name changed (a contact got a name, or the old file was
    named after the number) keeps one file. Only a file whose guids all belong
    to this chat is moved, and only onto a free name, or onto its own name
    in other capitals (one file on APFS). Returns True when a
    file was moved, so the caller rewrites it in full.
    """
    occupied = target_path.exists()
    for g in sorted(guids):
        for path, file_guids in index.get(g, []):
            if path == target_path or not path.exists():
                continue
            if not file_guids <= guids:
                continue  # that file also holds another chat: not ours to move
            if occupied:
                # Only a rename by capitals: the target name already opens
                # this very file. Rename it so the new capitals show.
                if path.name == target_path.name or not _same_file(path, target_path):
                    continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.rename(target_path)
            except OSError as e:
                _log_error(path, e)
                continue
            for lst in index.values():
                for i, (p, fg) in enumerate(lst):
                    if p == path:
                        lst[i] = (target_path, fg)
            return True
    return False


def _safe_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip()
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    if not cleaned:
        cleaned = "Unknown"
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip()
    return cleaned


def _is_unchanged(path: Path, last_message_ts: int, guids: set[str] | None = None) -> bool:
    """Return True if the existing vault file already covers up to last_message_ts.

    With guids, the file must also hold exactly those chats: a chat that just
    joined this person's file makes it changed, so it gets written in.
    """
    if not path.exists():
        return False
    if guids is not None and _file_guids(path) != guids:
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
    counts: dict[str, int] | None = None,
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
    if counts is not None:
        counts[chat["chat_guid"]] = len(rendered_messages)

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
