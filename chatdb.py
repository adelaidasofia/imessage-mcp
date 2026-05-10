"""chatdb.py — read layer for ~/Library/Messages/chat.db.

Single source for opening the SQLite database, converting Apple absolute time
to Unix seconds, and decoding NSKeyedArchiver attributedBody blobs (newer
macOS messages store text there instead of the `text` column).

Functions return plain dicts and lists. Higher modules format for output.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# Apple absolute time epoch: 2001-01-01 UTC = 978307200 unix seconds.
APPLE_EPOCH_OFFSET = 978307200
NANO_THRESHOLD = 100_000_000_000  # > ~3 yrs of seconds → nanoseconds


class FDADenied(RuntimeError):
    """Raised when chat.db cannot be opened due to TCC (Full Disk Access)."""


def apple_to_unix(apple_ts: int | float | None) -> int:
    """Convert Apple absolute time to Unix seconds. Handles ns and s storage."""
    if not apple_ts:
        return 0
    val = int(apple_ts)
    if val > NANO_THRESHOLD:
        val //= 1_000_000_000
    return val + APPLE_EPOCH_OFFSET


def unix_to_apple_ns(unix_ts: int | float) -> int:
    """Convert Unix seconds to Apple absolute time in nanoseconds (modern format)."""
    return int((unix_ts - APPLE_EPOCH_OFFSET) * 1_000_000_000)


@contextmanager
def open_db() -> Iterator[sqlite3.Connection]:
    """Open chat.db read-only and immutable. Yields a Connection.
    Raises FDADenied if TCC blocks the read.
    """
    if not CHAT_DB.exists():
        raise FileNotFoundError(f"chat.db not found at {CHAT_DB}")
    uri = f"file:{CHAT_DB}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        # macOS TCC denial surfaces in three forms depending on the calling
        # context: "authorization denied" (direct API), "operation not
        # permitted" (POSIX), or "unable to open database file" (sqlite3).
        if any(
            kw in msg
            for kw in (
                "authorization",
                "permission",
                "denied",
                "operation not permitted",
                "unable to open database file",
            )
        ):
            raise FDADenied(
                "chat.db cannot be opened. Most likely cause: macOS Full Disk "
                "Access is not granted to the calling process. Grant FDA in "
                "System Settings → Privacy & Security → Full Disk Access to: "
                "/Applications/Claude.app (for the MCP server), /bin/bash "
                "(for the launchd export wrapper), and /opt/homebrew/bin/python3 "
                "(for any subprocess that re-reads chat.db)."
            ) from e
        raise
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def extract_attributed_body(blob: bytes | None) -> str:
    """Best-effort decoder for NSKeyedArchiver attributedBody blobs.

    macOS 13+ may write text to attributedBody only, leaving message.text NULL.
    The blob is a typedstream archive of NSAttributedString; the actual content
    lives near an NSString class marker followed by a length-prefixed UTF-8 run.

    Returns the empty string if extraction fails. Robust against truncation.
    """
    if not blob or len(blob) < 16:
        return ""

    pos = blob.find(b"NSString")
    if pos < 0:
        return ""
    pos += len(b"NSString")

    # The string content is preceded by a length encoding marker.
    # Common shapes after the NSString class entry (a few bytes of metadata,
    # then 0x01 0x2b or 0x84 0x01 0x2b, then the length byte(s)).
    seek_end = min(pos + 64, len(blob))
    text_len = -1
    body_start = -1

    i = pos
    while i < seek_end - 2:
        if blob[i] == 0x2b:  # '+' the typed-stream type indicator
            length_byte = blob[i + 1]
            if length_byte == 0x81 and i + 2 < len(blob):
                text_len = blob[i + 2]
                body_start = i + 3
                break
            if length_byte == 0x82 and i + 3 < len(blob):
                text_len = int.from_bytes(blob[i + 2 : i + 4], "big")
                body_start = i + 4
                break
            if 0x01 <= length_byte < 0x80:
                text_len = length_byte
                body_start = i + 2
                break
        i += 1

    if body_start < 0 or text_len <= 0:
        return ""

    text_end = min(body_start + text_len, len(blob))
    try:
        return blob[body_start:text_end].decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def message_text(row: sqlite3.Row | dict) -> str:
    """Return the visible text for a message row, falling back to attributedBody."""
    text = row["text"] if "text" in row.keys() else None
    if text:
        return text
    blob = row["attributedBody"] if "attributedBody" in row.keys() else None
    return extract_attributed_body(blob) if blob else ""


# ---------------------------------------------------------------------------
# Public read APIs (used by main.py + export.py + search_index.py)
# ---------------------------------------------------------------------------


def healthcheck() -> dict[str, Any]:
    """Return FDA + db reachability + basic counts."""
    out: dict[str, Any] = {
        "db_path": str(CHAT_DB),
        "fda_granted": False,
        "chat_count": 0,
        "message_count": 0,
        "voice_note_count": 0,
        "edit_count": 0,
        "reply_thread_count": 0,
        "error": None,
    }
    try:
        with open_db() as conn:
            out["fda_granted"] = True
            out["chat_count"] = conn.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
            out["message_count"] = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            out["voice_note_count"] = conn.execute(
                "SELECT COUNT(*) FROM message WHERE is_audio_message = 1"
            ).fetchone()[0]
            out["edit_count"] = conn.execute(
                "SELECT COUNT(*) FROM message WHERE date_edited > 0"
            ).fetchone()[0]
            out["reply_thread_count"] = conn.execute(
                "SELECT COUNT(*) FROM message WHERE thread_originator_guid IS NOT NULL"
            ).fetchone()[0]
    except FDADenied as e:
        out["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def list_chats(
    limit: int = 50,
    unread_only: bool = False,
    include_groups: bool = True,
    days_back: int | None = None,
) -> list[dict[str, Any]]:
    """List chats sorted by most recent activity.

    chat.style: 45 = direct (1:1), 43 = group.
    """
    style_clause = "(43, 45)" if include_groups else "(45)"
    where = [f"c.style IN {style_clause}"]
    params: list[Any] = []

    if days_back is not None and days_back > 0:
        cutoff_ns = unix_to_apple_ns(_now() - days_back * 86400)
        where.append("EXISTS (SELECT 1 FROM chat_message_join cmj2 JOIN message m2 "
                     "ON m2.ROWID = cmj2.message_id WHERE cmj2.chat_id = c.ROWID "
                     "AND m2.date >= ?)")
        params.append(cutoff_ns)

    sql = f"""
        SELECT
          c.ROWID AS chat_rowid,
          c.guid AS chat_guid,
          c.style,
          c.service_name,
          c.chat_identifier,
          COALESCE(c.display_name, '') AS display_name,
          COALESCE(c.room_name, '') AS room_name,
          (SELECT COUNT(*) FROM chat_message_join cmj
             WHERE cmj.chat_id = c.ROWID) AS msg_count,
          (SELECT MAX(m.date) FROM chat_message_join cmj
             JOIN message m ON m.ROWID = cmj.message_id
             WHERE cmj.chat_id = c.ROWID) AS last_apple_date,
          (SELECT COUNT(*) FROM chat_message_join cmj
             JOIN message m ON m.ROWID = cmj.message_id
             WHERE cmj.chat_id = c.ROWID
               AND m.is_read = 0
               AND m.is_from_me = 0
               AND m.is_finished = 1
               AND m.item_type = 0
               AND m.is_system_message = 0) AS unread_count
        FROM chat c
        WHERE {' AND '.join(where)}
        ORDER BY last_apple_date DESC NULLS LAST
        LIMIT ?
    """
    params.append(limit if not unread_only else 1000)

    chats: list[dict[str, Any]] = []
    with open_db() as conn:
        for row in conn.execute(sql, params):
            entry = {
                "chat_rowid": row["chat_rowid"],
                "chat_guid": row["chat_guid"],
                "kind": "group" if row["style"] == 43 else "direct",
                "service": row["service_name"],
                "chat_identifier": row["chat_identifier"],
                "display_name": row["display_name"],
                "room_name": row["room_name"],
                "message_count": row["msg_count"] or 0,
                "last_message_ts": apple_to_unix(row["last_apple_date"]),
                "unread_count": row["unread_count"] or 0,
                "participants": [],
            }
            entry["participants"] = _participants_for_chat(conn, row["chat_rowid"])
            chats.append(entry)

    if unread_only:
        chats = [c for c in chats if c["unread_count"] > 0]
    return chats[:limit]


def _participants_for_chat(conn: sqlite3.Connection, chat_rowid: int) -> list[dict[str, str]]:
    sql = """
        SELECT h.id, h.service
        FROM chat_handle_join chj
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE chj.chat_id = ?
    """
    return [{"handle": r["id"], "service": r["service"]} for r in conn.execute(sql, (chat_rowid,))]


def get_chat(chat_id: int | str) -> dict[str, Any] | None:
    """Fetch one chat by ROWID or guid."""
    with open_db() as conn:
        if isinstance(chat_id, int) or (isinstance(chat_id, str) and chat_id.isdigit()):
            row = conn.execute("SELECT * FROM chat WHERE ROWID = ?", (int(chat_id),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM chat WHERE guid = ?", (chat_id,)).fetchone()
        if not row:
            return None
        msg_count = conn.execute(
            "SELECT COUNT(*) FROM chat_message_join WHERE chat_id = ?", (row["ROWID"],)
        ).fetchone()[0]
        last = conn.execute(
            "SELECT MAX(m.date) FROM chat_message_join cmj JOIN message m "
            "ON m.ROWID = cmj.message_id WHERE cmj.chat_id = ?",
            (row["ROWID"],),
        ).fetchone()[0]
        return {
            "chat_rowid": row["ROWID"],
            "chat_guid": row["guid"],
            "kind": "group" if row["style"] == 43 else "direct",
            "service": row["service_name"],
            "chat_identifier": row["chat_identifier"],
            "display_name": row["display_name"] or "",
            "room_name": row["room_name"] or "",
            "message_count": msg_count,
            "last_message_ts": apple_to_unix(last),
            "participants": _participants_for_chat(conn, row["ROWID"]),
        }


def list_messages(
    chat_id: int,
    limit: int = 50,
    before_rowid: int | None = None,
) -> list[dict[str, Any]]:
    """Pull messages for a chat ordered most-recent-first."""
    sql = """
        SELECT
          m.ROWID, m.guid, m.date, m.text, m.attributedBody,
          m.is_from_me, m.is_read, m.date_read, m.date_delivered,
          m.is_audio_message, m.handle_id, m.service,
          m.item_type, m.is_system_message,
          m.associated_message_guid, m.associated_message_type,
          m.associated_message_emoji,
          m.thread_originator_guid, m.reply_to_guid,
          m.date_edited, m.cache_has_attachments,
          h.id AS sender_handle, h.service AS sender_service
        FROM chat_message_join cmj
        JOIN message m ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ?
    """
    params: list[Any] = [chat_id]
    if before_rowid is not None:
        sql += " AND m.ROWID < ?"
        params.append(before_rowid)
    sql += " ORDER BY m.date DESC, m.ROWID DESC LIMIT ?"
    params.append(limit)

    out: list[dict[str, Any]] = []
    with open_db() as conn:
        for row in conn.execute(sql, params):
            out.append(_msg_row_to_dict(row))
    return out


def list_messages_chronological(chat_id: int) -> list[dict[str, Any]]:
    """Pull every message for a chat, oldest-first. Used by export."""
    sql = """
        SELECT
          m.ROWID, m.guid, m.date, m.text, m.attributedBody,
          m.is_from_me, m.is_read, m.date_read, m.date_delivered,
          m.is_audio_message, m.handle_id, m.service,
          m.item_type, m.is_system_message,
          m.associated_message_guid, m.associated_message_type,
          m.associated_message_emoji,
          m.thread_originator_guid, m.reply_to_guid,
          m.date_edited, m.cache_has_attachments,
          h.id AS sender_handle, h.service AS sender_service
        FROM chat_message_join cmj
        JOIN message m ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ?
        ORDER BY m.date ASC, m.ROWID ASC
    """
    with open_db() as conn:
        return [_msg_row_to_dict(r) for r in conn.execute(sql, (chat_id,))]


def get_message_context(rowid: int, before: int = 5, after: int = 5) -> list[dict[str, Any]]:
    """Return the chat-mate messages surrounding a given ROWID, ordered."""
    with open_db() as conn:
        chat = conn.execute(
            "SELECT chat_id FROM chat_message_join WHERE message_id = ? LIMIT 1", (rowid,)
        ).fetchone()
        if not chat:
            return []
        chat_id = chat["chat_id"]

        before_rows = list(
            conn.execute(
                """
                SELECT m.ROWID, m.guid, m.date, m.text, m.attributedBody, m.is_from_me,
                       m.is_read, m.date_read, m.date_delivered, m.is_audio_message,
                       m.handle_id, m.service, m.item_type, m.is_system_message,
                       m.associated_message_guid, m.associated_message_type,
                       m.associated_message_emoji, m.thread_originator_guid,
                       m.reply_to_guid, m.date_edited, m.cache_has_attachments,
                       h.id AS sender_handle, h.service AS sender_service
                FROM chat_message_join cmj
                JOIN message m ON m.ROWID = cmj.message_id
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE cmj.chat_id = ? AND m.ROWID < ?
                ORDER BY m.date DESC, m.ROWID DESC
                LIMIT ?
                """,
                (chat_id, rowid, before),
            )
        )
        center_row = conn.execute(
            """
            SELECT m.ROWID, m.guid, m.date, m.text, m.attributedBody, m.is_from_me,
                   m.is_read, m.date_read, m.date_delivered, m.is_audio_message,
                   m.handle_id, m.service, m.item_type, m.is_system_message,
                   m.associated_message_guid, m.associated_message_type,
                   m.associated_message_emoji, m.thread_originator_guid,
                   m.reply_to_guid, m.date_edited, m.cache_has_attachments,
                   h.id AS sender_handle, h.service AS sender_service
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.ROWID = ?
            """,
            (rowid,),
        ).fetchone()
        after_rows = list(
            conn.execute(
                """
                SELECT m.ROWID, m.guid, m.date, m.text, m.attributedBody, m.is_from_me,
                       m.is_read, m.date_read, m.date_delivered, m.is_audio_message,
                       m.handle_id, m.service, m.item_type, m.is_system_message,
                       m.associated_message_guid, m.associated_message_type,
                       m.associated_message_emoji, m.thread_originator_guid,
                       m.reply_to_guid, m.date_edited, m.cache_has_attachments,
                       h.id AS sender_handle, h.service AS sender_service
                FROM chat_message_join cmj
                JOIN message m ON m.ROWID = cmj.message_id
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE cmj.chat_id = ? AND m.ROWID > ?
                ORDER BY m.date ASC, m.ROWID ASC
                LIMIT ?
                """,
                (chat_id, rowid, after),
            )
        )

    rows = list(reversed(before_rows)) + ([center_row] if center_row else []) + after_rows
    return [_msg_row_to_dict(r) for r in rows if r]


def get_unread(limit: int = 50) -> list[dict[str, Any]]:
    """Return unread inbound messages across all chats, newest-first."""
    sql = """
        SELECT
          m.ROWID, m.guid, m.date, m.text, m.attributedBody,
          m.is_from_me, m.is_read, m.date_read, m.date_delivered,
          m.is_audio_message, m.handle_id, m.service,
          m.item_type, m.is_system_message,
          m.associated_message_guid, m.associated_message_type,
          m.associated_message_emoji,
          m.thread_originator_guid, m.reply_to_guid,
          m.date_edited, m.cache_has_attachments,
          h.id AS sender_handle, h.service AS sender_service,
          cmj.chat_id
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.is_read = 0
          AND m.is_from_me = 0
          AND m.is_finished = 1
          AND m.item_type = 0
          AND m.is_system_message = 0
        ORDER BY m.date DESC
        LIMIT ?
    """
    with open_db() as conn:
        out = []
        for row in conn.execute(sql, (limit,)):
            d = _msg_row_to_dict(row)
            d["chat_id"] = row["chat_id"]
            out.append(d)
    return out


def get_thread(originator_guid: str) -> list[dict[str, Any]]:
    """Return every message in a reply thread, oldest-first."""
    sql = """
        SELECT
          m.ROWID, m.guid, m.date, m.text, m.attributedBody,
          m.is_from_me, m.is_read, m.date_read, m.date_delivered,
          m.is_audio_message, m.handle_id, m.service,
          m.item_type, m.is_system_message,
          m.associated_message_guid, m.associated_message_type,
          m.associated_message_emoji,
          m.thread_originator_guid, m.reply_to_guid,
          m.date_edited, m.cache_has_attachments,
          h.id AS sender_handle, h.service AS sender_service
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE m.thread_originator_guid = ? OR m.guid = ?
        ORDER BY m.date ASC
    """
    with open_db() as conn:
        return [_msg_row_to_dict(r) for r in conn.execute(sql, (originator_guid, originator_guid))]


def list_attachments(
    chat_id: int | None = None,
    days_back: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return attachments, optionally filtered to one chat or recent days."""
    where = ["1 = 1"]
    params: list[Any] = []
    if chat_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM message_attachment_join maj2 "
            "JOIN chat_message_join cmj ON cmj.message_id = maj2.message_id "
            "WHERE maj2.attachment_id = a.ROWID AND cmj.chat_id = ?)"
        )
        params.append(chat_id)
    if days_back is not None and days_back > 0:
        cutoff_ns = unix_to_apple_ns(_now() - days_back * 86400)
        where.append("a.created_date >= ?")
        params.append(cutoff_ns)

    sql = f"""
        SELECT a.ROWID, a.guid, a.filename, a.mime_type, a.uti, a.transfer_name,
               a.total_bytes, a.is_outgoing, a.is_sticker, a.created_date,
               a.hide_attachment, a.transfer_state
        FROM attachment a
        WHERE {' AND '.join(where)}
        ORDER BY a.created_date DESC
        LIMIT ?
    """
    params.append(limit)
    with open_db() as conn:
        return [
            {
                "rowid": r["ROWID"],
                "guid": r["guid"],
                "filename": r["filename"] or "",
                "transfer_name": r["transfer_name"] or "",
                "mime_type": r["mime_type"] or "",
                "uti": r["uti"] or "",
                "total_bytes": r["total_bytes"] or 0,
                "is_outgoing": bool(r["is_outgoing"]),
                "is_sticker": bool(r["is_sticker"]),
                "created_ts": apple_to_unix(r["created_date"]),
            }
            for r in conn.execute(sql, params)
        ]


def attachments_for_message(conn: sqlite3.Connection, message_rowid: int) -> list[dict[str, Any]]:
    """Internal helper: return attachments joined to a message."""
    sql = """
        SELECT a.ROWID, a.guid, a.filename, a.mime_type, a.uti, a.transfer_name,
               a.total_bytes, a.is_outgoing
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE maj.message_id = ?
    """
    return [
        {
            "rowid": r["ROWID"],
            "guid": r["guid"],
            "filename": r["filename"] or "",
            "transfer_name": r["transfer_name"] or "",
            "mime_type": r["mime_type"] or "",
            "uti": r["uti"] or "",
            "total_bytes": r["total_bytes"] or 0,
            "is_outgoing": bool(r["is_outgoing"]),
        }
        for r in conn.execute(sql, (message_rowid,))
    ]


def _msg_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    text = message_text(row)
    out: dict[str, Any] = {
        "rowid": row["ROWID"],
        "guid": row["guid"],
        "ts": apple_to_unix(row["date"]),
        "text": text,
        "is_from_me": bool(row["is_from_me"]),
        "is_read": bool(row["is_read"]),
        "is_audio_message": bool(row["is_audio_message"]),
        "service": row["service"] or "",
        "item_type": row["item_type"] or 0,
        "is_system_message": bool(row["is_system_message"]),
        "sender_handle": row["sender_handle"] or "",
        "sender_service": row["sender_service"] or "",
        "associated_message_guid": row["associated_message_guid"] or "",
        "associated_message_type": row["associated_message_type"] or 0,
        "associated_message_emoji": row["associated_message_emoji"] or "",
        "thread_originator_guid": row["thread_originator_guid"] or "",
        "reply_to_guid": row["reply_to_guid"] or "",
        "date_edited_ts": apple_to_unix(row["date_edited"]),
        "date_read_ts": apple_to_unix(row["date_read"]) if "date_read" in row.keys() else 0,
        "date_delivered_ts": apple_to_unix(row["date_delivered"]) if "date_delivered" in row.keys() else 0,
        "has_attachments": bool(row["cache_has_attachments"]),
    }
    return out


def search_contacts_by_handle(
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return handles matching by raw substring (callers do display-name enrichment)."""
    if not query:
        return []
    q = f"%{query}%"
    sql = """
        SELECT h.ROWID, h.id, h.service,
               (SELECT COUNT(*) FROM message m WHERE m.handle_id = h.ROWID) AS msg_count,
               (SELECT MAX(m.date) FROM message m WHERE m.handle_id = h.ROWID) AS last_apple_date
        FROM handle h
        WHERE h.id LIKE ?
        ORDER BY last_apple_date DESC
        LIMIT ?
    """
    with open_db() as conn:
        return [
            {
                "handle_rowid": r["ROWID"],
                "handle": r["id"],
                "service": r["service"] or "",
                "message_count": r["msg_count"] or 0,
                "last_message_ts": apple_to_unix(r["last_apple_date"]),
                "is_email": "@" in (r["id"] or ""),
            }
            for r in conn.execute(sql, (q, limit))
        ]


def _now() -> int:
    import time
    return int(time.time())


# Lightweight CLI for smoke tests: `python3 chatdb.py healthcheck`.
if __name__ == "__main__":
    import json
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "healthcheck"
    if cmd == "healthcheck":
        print(json.dumps(healthcheck(), indent=2, default=str))
    elif cmd == "list_chats":
        print(json.dumps(list_chats(limit=int(sys.argv[2]) if len(sys.argv) > 2 else 10), indent=2, default=str))
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)
