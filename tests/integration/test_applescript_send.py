"""Tests for applescript_send._send_via_applescript, the outbound send path.

Nothing in this repo executed the send path before this file. Every failure it
guards against is an AppleScript COMPILE error or a hang, which means the
message never leaves the machine and the caller sees an opaque osascript error
rather than a send failure. That is the worst shape for a bug in a send path:
it looks like Messages.app said no.

osascript only exists on macOS, so every test here monkeypatches
subprocess.run and asserts on the script that would have been handed to it.
Nothing is sent, and the file runs anywhere CI does.

What each test pins down:
  1. Non-ASCII survives as itself (one emoji used to kill the whole send)
  2. Line breaks are spliced as `linefeed`, never escaped into a literal
  3. Quotes and backslashes are still escaped, so text cannot break the literal
  4. Chat guids are told apart from plain handles
  5. A chat guid targets a `chat`, because a group is never a `buddy`
  6. A plain handle targets a service-scoped buddy, never the global walk
  7. The service is an unquoted enum constant, and an unknown one falls back
  8. A non-zero osascript exit is raised rather than swallowed
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import applescript_send


@pytest.fixture
def captured_script(monkeypatch):
    """Capture the AppleScript instead of running osascript."""
    box: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "osascript", f"expected osascript, got {cmd[0]!r}"
        box["script"] = cmd[2]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(applescript_send.subprocess, "run", fake_run)
    return box


def test_non_ascii_survives_as_itself():
    """ensure_ascii would emit \\uXXXX, which AppleScript cannot parse at all."""
    rendered = applescript_send._as_string("Hey \U0001F916 there")

    assert "\U0001F916" in rendered
    assert "\\u" not in rendered


def test_line_breaks_become_linefeed_not_an_escape():
    """An AppleScript string literal cannot hold a line break in any form."""
    rendered = applescript_send._as_string("line one\nline two")

    assert " & linefeed & " in rendered
    assert "\\n" not in rendered
    assert "\n" not in rendered


def test_quotes_and_backslashes_are_still_escaped():
    """Keeping characters literal must not let text break out of the literal."""
    rendered = applescript_send._as_string('say "hi" \\ bye')

    assert '\\"' in rendered
    assert "\\\\" in rendered
    # Opening and closing quote plus the two escaped inner ones, and no more.
    assert rendered.count('"') == rendered.count('\\"') + 2


@pytest.mark.parametrize(
    "recipient,expected",
    [
        ("iMessage;+;chat123456789", True),
        ("any;+;chat987654321", True),
        ("iMessage;-;+16503086541", True),
        ("+16503086541", False),
        ("someone@example.com", False),
    ],
)
def test_chat_guids_are_told_apart_from_handles(recipient, expected):
    assert applescript_send._is_chat_guid(recipient) is expected


def test_a_chat_guid_targets_a_chat_never_a_buddy(captured_script):
    """A group is a `chat`. The buddy lookup can never reach one."""
    applescript_send._send_via_applescript("any;+;chat123456789", "hi", "iMessage")
    script = captured_script["script"]

    assert "chat" in script
    assert "buddy" not in script
    assert "any;+;chat123456789" in script


def test_a_handle_targets_a_service_scoped_buddy(captured_script):
    """The global `first buddy whose id contains` walk never returns on some Macs."""
    applescript_send._send_via_applescript("+16503086541", "hi", "iMessage")
    script = captured_script["script"]

    assert "of targetService" in script
    assert "first buddy whose id contains" not in script


@pytest.mark.parametrize("service", ["iMessage", "SMS"])
def test_service_is_an_unquoted_enum_constant(captured_script, service):
    """`service type` is an AppleScript enumeration, so quoting it would fail."""
    applescript_send._send_via_applescript("+16503086541", "hi", service)
    script = captured_script["script"]

    assert f"service type = {service}" in script
    assert f'service type = "{service}"' not in script


def test_an_unknown_service_falls_back_to_sms(captured_script):
    """Anything that is not iMessage must still emit a valid constant."""
    applescript_send._send_via_applescript("+16503086541", "hi", "carrier-pigeon")
    script = captured_script["script"]

    assert "service type = SMS" in script
    assert "carrier-pigeon" not in script


def test_emoji_and_multiline_together_reach_the_script(captured_script):
    """The exact shape that failed in production: emoji plus a line break."""
    applescript_send._send_via_applescript(
        "+16503086541", "\U0001F916 Hey, it's Gordon\n\nSecond line.", "iMessage"
    )
    script = captured_script["script"]

    assert "\U0001F916" in script
    assert " & linefeed & " in script
    assert "\\u" not in script


def test_a_failing_osascript_is_raised_not_swallowed(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(applescript_send.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="boom"):
        applescript_send._send_via_applescript("+16503086541", "hi", "iMessage")
