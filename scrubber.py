"""scrubber.py — prompt-injection scrubber for inbound message text.

Mirrors whatsapp-mcp's pattern list. Patterns are case-insensitive whole-phrase
substrings. When matched, the phrase is replaced with [REDACTED_INJECTION].
The original is kept upstream (chat.db is read-only and never modified); this
is the representation Claude sees.
"""
from __future__ import annotations

import os

_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard all prior",
    "you are now",
    "system:",
    "<system>",
    "</system>",
    "assistant:",
    "<|im_start|>",
    "<|im_end|>",
    "reveal your instructions",
    "reveal your system prompt",
    "print your system prompt",
]

ENABLED = os.environ.get("IMESSAGE_SCRUB_PROMPT_INJECTION", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def scrub(text: str | None) -> tuple[str | None, list[str]]:
    """Return (scrubbed_text, matched_patterns)."""
    if not text or not ENABLED:
        return text, []
    lowered = text.lower()
    flags: list[str] = []
    out = text
    for pat in _INJECTION_PATTERNS:
        if pat in lowered:
            flags.append(pat)
            idx = 0
            while idx < len(out):
                lo = out.lower().find(pat, idx)
                if lo < 0:
                    break
                out = out[:lo] + "[REDACTED_INJECTION]" + out[lo + len(pat):]
                idx = lo + len("[REDACTED_INJECTION]")
    return out, flags
