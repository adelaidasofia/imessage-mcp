"""audit.py — append-only JSONL audit log for tool invocations.

One line per tool call. The hooks runtime + a quarterly review can replay
this to spot patterns. Redacts nothing at this layer; payload bodies are
opaque and may carry message text.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

ENABLED = os.environ.get("IMESSAGE_AUDIT_LOG", "true").lower() in ("1", "true", "yes", "on")
PATH = Path(
    os.environ.get(
        "IMESSAGE_AUDIT_LOG_PATH",
        str(Path.home() / ".claude" / "imessage-mcp" / "audit.log"),
    )
)


def log(
    tool: str,
    params: dict[str, Any],
    result_summary: str,
    duration_ms: int,
    error: str | None = None,
) -> None:
    if not ENABLED:
        return
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "tool": tool,
            "params": params,
            "result_summary": result_summary,
            "duration_ms": duration_ms,
            "error": error,
        }
        with PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
