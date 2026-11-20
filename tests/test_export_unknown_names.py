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


def _bodies(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _files(root))


def test_email_spellings_are_one_chat_file_not_overwritten(env):
    root, state = env
    state["chats"] = [
        _chat(1, "g-up", "Someone@Example.com"),
        _chat(2, "g-low", "someone@example.com"),
    ]
    export.export_to_vault()
    files = _files(root)
    assert len(files) == 1, files
    assert _guids(files[0]) == {"g-up", "g-low"}  # both chats' messages kept
    assert frontmatter.load(str(files[0])).metadata["message_count"] == 2


def test_groups_with_the_same_name_get_one_file_each(env):
    root, state = env
    state["names"] = {NAMED: "Alex Rivera", PHONE_A: "Sam Lee"}
    state["chats"] = [
        _chat(1, "g-f1", "chat111111111", kind="group", participants=[NAMED], display_name="Familia"),
        _chat(2, "g-f2", "chat222222222", kind="group", participants=[PHONE_A], display_name="familia"),
        _chat(3, "g-m1", "chat333333333", kind="group", participants=[NAMED, PHONE_A]),
        _chat(4, "g-m2", "chat444444444", kind="group", participants=[NAMED, PHONE_A]),
        _chat(5, "g-solo", "chat555555555", kind="group", participants=[PHONE_A], display_name="Trabajo"),
    ]
    export.export_to_vault()
    files = _files(root / "Groups")
    assert len(files) == 5, files
    assert {frozenset(_guids(f)) for f in files} == {frozenset({g}) for g in ("g-f1", "g-f2", "g-m1", "g-m2", "g-solo")}
    assert (root / "Groups" / "Trabajo.md").exists()  # no clash, no id


def test_ids_do_not_depend_on_include_groups(env):
    root, state = env
    state["chats"] = [_chat(1, "g-a", PHONE_A), _chat(2, "g-grp", "chat1", kind="group", participants=[PHONE_B])]
    export.export_to_vault(include_groups=True)
    with_groups = sorted(f.name for f in _files(root) if f.parent == root)
    for f in _files(root):
        f.unlink()
    state["chats"] = [c for c in state["chats"] if c["kind"] != "group"]
    export.export_to_vault(include_groups=False)
    assert sorted(f.name for f in _files(root) if f.parent == root) == with_groups


def test_chat_joining_a_named_person_is_absorbed_and_old_file_retired(env):
    root, state = env
    state["names"] = {NAMED: "Alex Rivera"}
    state["chats"] = [_chat(1, "g-a", PHONE_A), _chat(2, "g-n", NAMED)]
    export.export_to_vault()
    assert len(_files(root)) == 2
    # Now the unknown number turns out to be Alex too.
    state["names"] = {NAMED: "Alex Rivera", PHONE_A: "Alex Rivera"}
    export.export_to_vault()
    files = _files(root)
    assert [f.name for f in files] == ["Alex Rivera.md"], files
    assert _guids(files[0]) == {"g-a", "g-n"}
    assert frontmatter.load(str(files[0])).metadata["message_count"] == 2


def test_leftover_with_history_the_database_lacks_is_kept(env):
    root, state = env
    state["names"] = {NAMED: "Alex Rivera", PHONE_A: "Alex Rivera"}
    state["chats"] = [_chat(1, "g-a", PHONE_A), _chat(2, "g-n", NAMED)]
    (root / "Alex Rivera.md").write_text("---\ntype: imessage-chat\nchat_guid: g-n\n---\n\nx\n", encoding="utf-8")
    old = root / "Chat with an unknown number (iMessage) old.md"
    old.write_text("---\ntype: imessage-chat\nchat_guid: g-a\nmessage_count: 40\n---\n\nolder\n", encoding="utf-8")
    export.export_to_vault()
    assert old.exists()


def _total_messages(root: Path) -> int:
    return sum(int(frontmatter.load(str(p)).metadata.get("message_count") or 0) for p in _files(root))


def test_rename_by_capitals_keeps_the_file_and_its_messages(env):
    root, state = env
    state["names"] = {PHONE_A: "alex rivera"}
    state["chats"] = [_chat(1, "g-a", PHONE_A)]
    export.export_to_vault()
    assert [f.name for f in _files(root)] == ["alex rivera.md"]
    state["names"] = {PHONE_A: "Alex Rivera"}
    export.export_to_vault()
    files = _files(root)
    assert len(files) == 1, files
    assert files[0].name == "Alex Rivera.md"
    assert _guids(files[0]) == {"g-a"}
    assert _total_messages(root) == 1


def test_person_with_two_chats_renamed_by_capitals_keeps_all_messages(env):
    root, state = env
    state["names"] = {PHONE_A: "alex rivera", EMAIL: "alex rivera"}
    state["chats"] = [_chat(1, "g-a", PHONE_A), _chat(2, "g-e", EMAIL)]
    export.export_to_vault()
    assert [f.name for f in _files(root)] == ["alex rivera.md"]
    state["names"] = {PHONE_A: "Alex Rivera", EMAIL: "Alex Rivera"}
    export.export_to_vault()
    files = _files(root)
    assert len(files) == 1, files
    assert files[0].name == "Alex Rivera.md"
    assert _guids(files[0]) == {"g-a", "g-e"}
    assert _total_messages(root) == 2


def test_name_matching_another_file_by_capitals_does_not_overwrite_it(env):
    root, state = env
    note = root / "Alex Rivera.md"
    note.write_text("---\ntitle: my own note\n---\n\nmine\n", encoding="utf-8")
    state["names"] = {PHONE_A: "alex rivera"}
    state["chats"] = [_chat(1, "g-a", PHONE_A)]
    export.export_to_vault()
    assert note.read_text(encoding="utf-8").endswith("mine\n")
    chat_files = [f for f in _files(root) if _guids(f) == {"g-a"}]
    assert len(chat_files) == 1
