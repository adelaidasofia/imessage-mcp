"""contacts.py — resolve real names from the macOS Contacts.app database.

Contacts.app stores its records in `~/Library/Application Support/AddressBook/`
across one or more `Sources/<UUID>/AddressBook-v22.abcddb` SQLite files (one
per account: iCloud, Google, On-My-Mac, etc.).

We expose `resolve_handle(handle)` which returns a best-guess display name
for a phone or email handle. Returns None if no match.

Permission: AddressBook is gated by TCC (Contacts privacy). We fail soft when
the database is unreadable so callers can fall back to the raw handle.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ADDRESSBOOK_ROOT = Path.home() / "Library" / "Application Support" / "AddressBook"

# Cached lookup: avoid re-walking every call.
_CACHE: dict[str, str | None] = {}
_CACHE_TS = 0.0
_CACHE_TTL_SEC = 300.0


def _digits_only(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _addressbook_dbs() -> list[Path]:
    if not ADDRESSBOOK_ROOT.is_dir():
        return []
    out = []
    for p in ADDRESSBOOK_ROOT.rglob("AddressBook-v22.abcddb"):
        out.append(p)
    main = ADDRESSBOOK_ROOT / "AddressBook-v22.abcddb"
    if main.exists() and main not in out:
        out.append(main)
    return out


@contextmanager
def _open_ab(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _full_name(row: sqlite3.Row) -> str:
    first = (row["ZFIRSTNAME"] if "ZFIRSTNAME" in row.keys() else "") or ""
    last = (row["ZLASTNAME"] if "ZLASTNAME" in row.keys() else "") or ""
    nick = (row["ZNICKNAME"] if "ZNICKNAME" in row.keys() else "") or ""
    org = (row["ZORGANIZATION"] if "ZORGANIZATION" in row.keys() else "") or ""
    name = " ".join(p for p in (first, last) if p).strip()
    if not name and nick:
        name = nick
    if not name and org:
        name = org
    return name


def _build_cache() -> dict[str, str]:
    """Walk every AddressBook DB and emit handle → name mappings."""
    cache: dict[str, str] = {}
    for db_path in _addressbook_dbs():
        try:
            with _open_ab(db_path) as conn:
                # Phones
                try:
                    sql = """
                        SELECT p.ZFULLNUMBER AS num,
                               r.ZFIRSTNAME, r.ZLASTNAME, r.ZNICKNAME, r.ZORGANIZATION
                        FROM ZABCDPHONENUMBER p
                        JOIN ZABCDRECORD r ON r.Z_PK = p.ZOWNER
                    """
                    for row in conn.execute(sql):
                        if not row["num"]:
                            continue
                        digits = _digits_only(row["num"])
                        name = _full_name(row)
                        if digits and name and digits not in cache:
                            cache[digits] = name
                            # Also map E.164 with leading + and last-7 fallback.
                            cache["+" + digits] = name
                            if len(digits) >= 7:
                                cache[digits[-7:]] = cache.get(digits[-7:], name)
                except sqlite3.OperationalError:
                    pass
                # Emails
                try:
                    sql = """
                        SELECT e.ZADDRESSNORMALIZED AS addr,
                               r.ZFIRSTNAME, r.ZLASTNAME, r.ZNICKNAME, r.ZORGANIZATION
                        FROM ZABCDEMAILADDRESS e
                        JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER
                    """
                    for row in conn.execute(sql):
                        addr = (row["addr"] or "").strip().lower()
                        name = _full_name(row)
                        if addr and name and addr not in cache:
                            cache[addr] = name
                except sqlite3.OperationalError:
                    pass
        except Exception:
            continue
    return cache


def _ensure_cache() -> dict[str, str | None]:
    global _CACHE_TS
    now = time.time()
    if not _CACHE or now - _CACHE_TS > _CACHE_TTL_SEC:
        _CACHE.clear()
        _CACHE.update(_build_cache())
        _CACHE_TS = now
    return _CACHE


def resolve_handle(handle: str | None) -> str | None:
    """Return a display name for a phone or email handle, or None."""
    if not handle:
        return None
    h = handle.strip()
    cache = _ensure_cache()
    if "@" in h:
        return cache.get(h.lower())
    digits = _digits_only(h)
    if not digits:
        return None
    if h in cache:
        return cache[h]
    if digits in cache:
        return cache[digits]
    if ("+" + digits) in cache:
        return cache["+" + digits]
    if len(digits) >= 7 and digits[-7:] in cache:
        return cache[digits[-7:]]
    return None


def resolve_handles(handles: list[str]) -> dict[str, str | None]:
    return {h: resolve_handle(h) for h in handles}


if __name__ == "__main__":
    import json
    import sys

    cache = _ensure_cache()
    print(json.dumps({"contacts_indexed": len(cache), "sample": list(cache.items())[:5]}, indent=2))
    if len(sys.argv) > 1:
        print(json.dumps({"resolved": resolve_handle(sys.argv[1])}))
