"""search_index.py — incremental FTS5 search over chat.db message bodies.

We never modify chat.db. Instead we maintain a sibling SQLite at
`~/.claude/imessage-mcp/search.db` with an FTS5 virtual table mirroring the
text content of each message. Updates are incremental: track the highest
message.ROWID we've ingested and pull only newer rows on each refresh.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import chatdb

INDEX_DB = Path(
    os.environ.get(
        "IMESSAGE_SEARCH_DB",
        str(Path.home() / ".claude" / "imessage-mcp" / "search.db"),
    )
)


def _open_index() -> sqlite3.Connection:
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INDEX_DB), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # FTS5 reserves `rowid` as the implicit primary key. We store the chat.db
    # message ROWID directly as the FTS rowid, so searches return it naturally.
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
        USING fts5(
            text,
            sender_handle UNINDEXED,
            chat_id UNINDEXED,
            ts UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS index_state (
            key TEXT PRIMARY KEY,
            value INTEGER
        )
        """
    )
    return conn


def _last_indexed_rowid(idx: sqlite3.Connection) -> int:
    row = idx.execute(
        "SELECT value FROM index_state WHERE key = 'last_message_rowid'"
    ).fetchone()
    return int(row[0]) if row else 0


def _set_last_indexed_rowid(idx: sqlite3.Connection, value: int) -> None:
    idx.execute(
        "INSERT OR REPLACE INTO index_state (key, value) VALUES ('last_message_rowid', ?)",
        (value,),
    )


def refresh_index(full_rebuild: bool = False) -> dict[str, Any]:
    """Pull messages with ROWID > last_indexed and add them to the FTS table."""
    started = time.time()
    added = 0
    max_seen = 0

    with _open_index() as idx, chatdb.open_db() as src:
        if full_rebuild:
            idx.execute("DELETE FROM messages_fts")
            idx.execute("DELETE FROM index_state")
            idx.commit()

        last = _last_indexed_rowid(idx)
        sql = """
            SELECT m.ROWID, m.text, m.attributedBody, m.handle_id, m.date,
                   h.id AS sender_handle, cmj.chat_id
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            WHERE m.ROWID > ?
            ORDER BY m.ROWID ASC
        """
        for row in src.execute(sql, (last,)):
            text = chatdb.message_text(row)
            if not text:
                max_seen = max(max_seen, row["ROWID"])
                continue
            idx.execute(
                """
                INSERT INTO messages_fts (rowid, text, sender_handle, chat_id, ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["ROWID"],
                    text,
                    row["sender_handle"] or "",
                    row["chat_id"] or 0,
                    chatdb.apple_to_unix(row["date"]),
                ),
            )
            added += 1
            max_seen = max(max_seen, row["ROWID"])

        if max_seen > last:
            _set_last_indexed_rowid(idx, max_seen)
        idx.commit()

    return {
        "added": added,
        "last_rowid": max_seen,
        "duration_ms": int((time.time() - started) * 1000),
        "index_path": str(INDEX_DB),
    }


def search(
    query: str,
    chat_id: int | None = None,
    sender: str | None = None,
    days_back: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """FTS5 search across indexed messages with optional filters."""
    if not query.strip():
        return []
    where = ["messages_fts MATCH ?"]
    params: list[Any] = [_sanitize_query(query)]
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    if sender:
        where.append("sender_handle = ?")
        params.append(sender)
    if days_back is not None and days_back > 0:
        cutoff = int(time.time()) - days_back * 86400
        where.append("ts >= ?")
        params.append(cutoff)

    sql = f"""
        SELECT rowid, text, sender_handle, chat_id, ts,
               snippet(messages_fts, 0, '[', ']', '…', 12) AS snippet
        FROM messages_fts
        WHERE {' AND '.join(where)}
        ORDER BY rank
        LIMIT ?
    """
    params.append(limit)

    with _open_index() as idx:
        return [
            {
                "rowid": r["rowid"],
                "text": r["text"],
                "snippet": r["snippet"],
                "sender_handle": r["sender_handle"],
                "chat_id": r["chat_id"],
                "ts": r["ts"],
            }
            for r in idx.execute(sql, params)
        ]


def _sanitize_query(q: str) -> str:
    """Quote each token to keep FTS5's punctuation/operator interpretation stable."""
    parts = [t for t in q.split() if t]
    if not parts:
        return ""
    return " ".join(f'"{p.replace(chr(34), "")}"' for p in parts)


def status() -> dict[str, Any]:
    try:
        with _open_index() as idx:
            count = idx.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
            last = _last_indexed_rowid(idx)
        return {
            "indexed_messages": count,
            "last_indexed_rowid": last,
            "index_path": str(INDEX_DB),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "index_path": str(INDEX_DB)}


if __name__ == "__main__":
    import json
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "rebuild":
        print(json.dumps(refresh_index(full_rebuild=True), indent=2))
    elif cmd == "refresh":
        print(json.dumps(refresh_index(), indent=2))
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "search":
        q = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        print(json.dumps(search(q, limit=10), indent=2))
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)
