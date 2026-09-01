"""Tests for chatdb.open_db, the read-only connection path.

Nothing in this repo executed open_db before this file, and CI runs on Linux
where ~/Library/Messages/chat.db does not exist, so the checks said nothing
about the connection behaviour. Every test here builds a synthetic database in
tmp_path and monkeypatches chatdb.CHAT_DB, so it runs anywhere.

What each test pins down:
  1. Rows still in the WAL are visible (the bug the immutable=1 open caused)
  2. Lock contention retries the fresh path and never downgrades to a stale one
  3. A real held lock does surface as SQLITE_BUSY, so (2) matches reality
  4. SQLITE_CANTOPEN does fall back, and says so in the log and the flag
  5. A file that is not a database raises without leaking the connection
  6. Both paths are still read-only
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import chatdb


class _CantOpen(sqlite3.OperationalError):
    """SQLITE_CANTOPEN as SQLite raises it: no -shm possible, so no WAL read.

    Injected rather than synthesized. The real trigger is a read-only volume or
    a copy with no WAL beside it, neither of which is portable to build in CI.
    Test 3 covers the classification against a genuine driver error.
    """

    sqlite_errorname = "SQLITE_CANTOPEN"


def _build_wal_db(path: Path, checkpointed: int = 50, wal_only: int = 4) -> sqlite3.Connection:
    """Build a WAL database split across the main file and the WAL.

    Returns the writer, which the caller must keep open and close last: the last
    connection to close checkpoints the WAL, which would erase the split this
    fixture exists to create.
    """
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL").fetchone()
    writer.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT)")
    writer.commit()

    writer.executemany(
        "INSERT INTO message (text) VALUES (?)", [(f"m{i}",) for i in range(checkpointed)]
    )
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    # These stay in chat.db-wal: autocheckpoint off, writer left open.
    writer.execute("PRAGMA wal_autocheckpoint=0").fetchone()
    writer.executemany(
        "INSERT INTO message (text) VALUES (?)", [(f"w{i}",) for i in range(wal_only)]
    )
    writer.commit()
    return writer


def _count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]


def _immutable_count(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        return _count(conn)
    finally:
        conn.close()


def test_open_db_reads_rows_still_in_the_wal(tmp_path, monkeypatch):
    """The regression test for the bug this PR fixes."""
    db = tmp_path / "chat.db"
    writer = _build_wal_db(db, checkpointed=50, wal_only=4)
    try:
        monkeypatch.setattr(chatdb, "CHAT_DB", db)

        with chatdb.open_db() as conn:
            assert _count(conn) == 54
            assert chatdb.reading_stale_snapshot() is False

        # The fixture genuinely reproduces the stale read, so the assertion
        # above is measuring something rather than passing by luck.
        assert _immutable_count(db) == 50
        # And the flag does not leak past the context manager.
        assert chatdb.reading_stale_snapshot() is False
    finally:
        writer.close()


def test_lock_contention_raises_instead_of_serving_a_stale_snapshot(tmp_path, monkeypatch):
    """A held lock must not silently downgrade the caller to the stale path.

    Rollback-journal mode on purpose: there an exclusive writer really does block
    readers, so the contention is genuine rather than mocked. immutable=1 ignores
    locking entirely and would happily return the short count, which is exactly
    the failure being guarded against.
    """
    db = tmp_path / "chat.db"
    writer = sqlite3.connect(db)
    try:
        writer.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT)")
        writer.commit()

        monkeypatch.setattr(chatdb, "CHAT_DB", db)
        monkeypatch.setattr(chatdb, "OPEN_TIMEOUT", 0.05)
        monkeypatch.setattr(chatdb, "BUSY_RETRIES", 2)
        monkeypatch.setattr(chatdb, "BUSY_BACKOFF", 0.01)

        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO message (text) VALUES ('held')")

        with pytest.raises(sqlite3.OperationalError) as excinfo, chatdb.open_db():
            pass

        assert "locked" in str(excinfo.value).lower()
        assert chatdb.reading_stale_snapshot() is False
        writer.rollback()
    finally:
        writer.close()


def test_a_held_lock_really_does_report_busy(tmp_path):
    """Pins the classification to the driver rather than to our own fake.

    _BUSY_ERRORS is matched on sqlite_errorname, so it is only correct if a real
    contended open actually carries SQLITE_BUSY. If SQLite ever renames it, this
    fails here instead of silently turning the retry branch into dead code.
    """
    db = tmp_path / "chat.db"
    writer = sqlite3.connect(db)
    try:
        writer.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT)")
        writer.commit()
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO message (text) VALUES ('held')")

        reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.05)
        try:
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                _count(reader)
        finally:
            reader.close()

        assert chatdb._errname(excinfo.value) in chatdb._BUSY_ERRORS
        writer.rollback()
    finally:
        writer.close()


def test_cantopen_falls_back_to_immutable_and_says_so(tmp_path, monkeypatch, caplog):
    db = tmp_path / "chat.db"
    writer = _build_wal_db(db, checkpointed=50, wal_only=4)
    try:
        monkeypatch.setattr(chatdb, "CHAT_DB", db)
        real_connect = sqlite3.connect
        attempted: list[str] = []

        def fake_connect(database, *args, **kwargs):
            attempted.append(str(database))
            if "immutable=1" not in str(database):
                raise _CantOpen("unable to open database file")
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(chatdb.sqlite3, "connect", fake_connect)

        with caplog.at_level(logging.WARNING, logger=chatdb.log.name), chatdb.open_db() as conn:
            assert chatdb.reading_stale_snapshot() is True
            # The stale snapshot itself: the four WAL rows are missing.
            assert _count(conn) == 50

        assert len(attempted) == 2, "expected one fresh attempt then the fallback"
        assert "immutable=1" not in attempted[0]
        assert "immutable=1" in attempted[1]

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("immutable=1" in m for m in warnings), warnings
        assert chatdb.reading_stale_snapshot() is False
    finally:
        writer.close()


def test_busy_never_reaches_the_immutable_fallback(tmp_path, monkeypatch):
    """The narrow point of finding 1: retry on busy, fall back only on cantopen."""

    class _Busy(sqlite3.OperationalError):
        sqlite_errorname = "SQLITE_BUSY"

    db = tmp_path / "chat.db"
    writer = _build_wal_db(db, checkpointed=50, wal_only=4)
    try:
        monkeypatch.setattr(chatdb, "CHAT_DB", db)
        monkeypatch.setattr(chatdb, "BUSY_RETRIES", 2)
        monkeypatch.setattr(chatdb, "BUSY_BACKOFF", 0.001)
        attempted: list[str] = []

        def always_busy(database, *args, **kwargs):
            attempted.append(str(database))
            raise _Busy("database is locked")

        monkeypatch.setattr(chatdb.sqlite3, "connect", always_busy)

        with pytest.raises(sqlite3.OperationalError), chatdb.open_db():
            pass

        assert len(attempted) == 3, "expected the initial attempt plus BUSY_RETRIES"
        assert not any("immutable=1" in uri for uri in attempted)
    finally:
        writer.close()


def test_a_file_that_is_not_a_database_raises_without_leaking(tmp_path, monkeypatch):
    """Finding 2: the probe widened the exception surface past OperationalError.

    DatabaseError used to escape the handler, so candidate.close() (which lived
    only in the except body) never ran.
    """
    db = tmp_path / "chat.db"
    db.write_bytes(b"not a database, just bytes" * 40)
    monkeypatch.setattr(chatdb, "CHAT_DB", db)

    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def spy_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(chatdb.sqlite3, "connect", spy_connect)

    with pytest.raises(sqlite3.DatabaseError), chatdb.open_db():
        pass

    assert opened, "expected at least one connection attempt"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_both_paths_are_read_only(tmp_path, monkeypatch):
    """Written down because the review checked it by hand.

    A change that restored freshness by opening for writes would pass every
    other test in this file.
    """
    db = tmp_path / "chat.db"
    writer = _build_wal_db(db, checkpointed=50, wal_only=4)
    try:
        monkeypatch.setattr(chatdb, "CHAT_DB", db)

        with chatdb.open_db() as conn:
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                conn.execute("INSERT INTO message (text) VALUES ('nope')")
            assert "readonly" in str(excinfo.value).lower()

        real_connect = sqlite3.connect

        def fake_connect(database, *args, **kwargs):
            if "immutable=1" not in str(database):
                raise _CantOpen("unable to open database file")
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(chatdb.sqlite3, "connect", fake_connect)

        with chatdb.open_db() as conn:
            assert chatdb.reading_stale_snapshot() is True
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                conn.execute("INSERT INTO message (text) VALUES ('nope')")
            assert "readonly" in str(excinfo.value).lower()
    finally:
        writer.close()
