"""tickets#6939: chats with no known name are not filed under their number.

Studio shows a chat file's name as its title, so a file named after a phone
number or chat id reads as a bare number. Fixture identities are synthetic
(555 numbers, example.com).
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import frontmatter
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import export  # noqa: E402

PHONE_A = "+15555550100"
PHONE_B = "+15555550199"
EMAIL = "someone@example.com"
NAMED = "+15555550123"


def _chat(rowid, guid, handle, kind="direct", participants=None, display_name=""):
    return {
        "chat_rowid": rowid,
        "chat_guid": guid,
        "chat_identifier": handle,
        "display_name": display_name,
        "kind": kind,
        "service": "iMessage",
        "message_count": 1,
        "last_message_ts": 1785000000 + rowid,
        "participants": [{"handle": h} for h in (participants or [handle])],
    }


def _msg(rowid, ts):
    return {
        "rowid": rowid, "guid": f"m{rowid}", "ts": ts, "text": "hola",
        "service": "iMessage", "is_from_me": 0, "sender_handle": "",
        "associated_message_type": 0, "associated_message_guid": None,
        "is_system_message": 0, "item_type": 0, "has_attachments": 0,
        "thread_originator_guid": None, "date_edited_ts": 0, "date_read_ts": 0,
        "is_audio_message": 0,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = {"chats": [], "names": {}}
    monkeypatch.setattr(export, "VAULT_OUT", tmp_path)
    monkeypatch.setattr(export.chatdb, "list_chats", lambda **kw: state["chats"])
    monkeypatch.setattr(
        export.chatdb, "list_messages_chronological",
        lambda rowid: [_msg(rowid, 1785000000 + rowid)],
    )
    monkeypatch.setattr(export.chatdb, "open_db", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(export.contacts, "resolve_handle", lambda h: state["names"].get(h))
    return tmp_path, state


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md"))


def _guids(path: Path) -> set[str]:
    return export._file_guids(path)


def test_unknown_chats_get_readable_unique_stable_names(env):
    root, state = env
    state["names"] = {NAMED: "Alex Rivera"}
    state["chats"] = [
        _chat(1, "g-a", PHONE_A),
        _chat(2, "g-b", PHONE_B),
        _chat(3, "g-e", EMAIL),
        _chat(4, "g-n", NAMED),
        _chat(5, "g-grp", "chat123456789", kind="group", participants=[PHONE_A, PHONE_B]),
        _chat(6, "g-grp2", "chat987654321", kind="group", participants=[NAMED, PHONE_A]),
    ]
    export.export_to_vault()
    names = {p.relative_to(root).as_posix(): p for p in _files(root)}
    idents = [PHONE_A[1:], PHONE_B[1:], NAMED[1:], "example.com", "123456789", "987654321"]
    for rel in names:
        assert "@" not in rel and not any(i in rel for i in idents), f"identifier in file name: {rel}"
    stems = sorted(names)
    assert "Alex Rivera.md" in stems
    assert sum(s.startswith("Groups/Alex Rivera + 1 more ") for s in stems) == 1
    assert sum(s.startswith("Chat with an unknown number (iMessage) ") for s in stems) == 2
    assert sum(s.startswith("Chat with an unknown email address (iMessage) ") for s in stems) == 1
    assert sum(s.startswith("Groups/Group chat (iMessage) ") for s in stems) == 1
    # The identifier stays in the frontmatter.
    phones = {frontmatter.load(str(p)).metadata.get("phones") for p in names.values()}
    assert {PHONE_A, PHONE_B} <= phones

    before = stems
    export.export_to_vault(force=True)
    assert sorted(p.relative_to(root).as_posix() for p in _files(root)) == before


def test_number_named_file_is_renamed_then_renamed_to_the_name(env):
    root, state = env
    state["chats"] = [_chat(1, "g-a", PHONE_A)]
    legacy = root / f"{PHONE_A}.md"
    legacy.write_text(
        "---\ntype: imessage-chat\ncontact: '" + PHONE_A + "'\nchat_guid: g-a\n"
        "last_message_ts: 9999999999\nnote_by_user: keep me\n---\n\nold\n",
        encoding="utf-8",
    )
    export.export_to_vault()
    files = _files(root)
    assert len(files) == 1, files
    assert files[0].name.startswith("Chat with an unknown number (iMessage) ")
    meta = frontmatter.load(str(files[0])).metadata
    assert meta["note_by_user"] == "keep me"  # same file moved, extras kept
    assert meta["message_count"] == 1  # rewritten after the move

    state["names"] = {PHONE_A: "Alex Rivera"}
    export.export_to_vault()
    files = _files(root)
    assert [f.name for f in files] == ["Alex Rivera.md"]
    assert _guids(files[0]) == {"g-a"}


def test_file_holding_another_chat_is_not_moved(env):
    root, state = env
    state["chats"] = [_chat(1, "g-a", PHONE_A)]
    shared = root / "Someone.md"
    shared.write_text(
        "---\ntype: imessage-chat\nchat_guids: [g-a, g-other]\n---\n\nx\n", encoding="utf-8"
    )
    export.export_to_vault()
    assert shared.exists()
    assert len(_files(root)) == 2


def test_groups_with_the_same_known_members_do_not_share_a_file(env):
    root, state = env
    state["names"] = {NAMED: "Alex Rivera"}
    state["chats"] = [
        _chat(1, "g-1", "chat111111111", kind="group", participants=[NAMED, PHONE_A]),
        _chat(2, "g-2", "chat222222222", kind="group", participants=[NAMED, PHONE_B]),
    ]
    export.export_to_vault()
    files = _files(root / "Groups")
    assert len(files) == 2, files
    assert {frozenset(_guids(f)) for f in files} == {frozenset({"g-1"}), frozenset({"g-2"})}


@pytest.mark.parametrize("name,want", [
    ("+1 555 555 0199", True), ("12345", True), ("someone@example.com", True),
    ("chat123456", True), ("", True), ("Alex Rivera", False), ("Familia 2026", False),
])
def test_looks_like_identifier(name, want):
    assert export._looks_like_identifier(name) is want
