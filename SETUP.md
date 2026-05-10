# SETUP

## 1. Full Disk Access — REQUIRED, three paths

The MCP, the export wrapper, and the launchd agent all read `~/Library/Messages/chat.db`. macOS TCC gates that file. Each spawning process needs FDA explicitly — TCC does not inherit reliably across user contexts.

**System Settings → Privacy & Security → Full Disk Access → enable for:**

1. `/Applications/Claude.app` — the MCP server runs as a Claude.app subprocess and inherits this grant
2. `/bin/bash` — the launchd agent shell needs this to run the export wrapper at the 4h cadence and at boot
3. `/opt/homebrew/bin/python3` (Apple Silicon) and `/usr/bin/python3` — any other process that imports `chatdb`

To add `/bin/bash`: click `+` in the FDA list, press `Cmd+Shift+G`, type `/bin/bash`, press Return, click Open. Toggle the new entry on.

Symptom of missing grant: `sqlite3.OperationalError: unable to open database file` in the export logs. The MCP healthcheck returns `fda_granted: false` with a remediation message.

Verify each path:
```bash
# Shell (this terminal):
sqlite3 -readonly ~/Library/Messages/chat.db "SELECT COUNT(*) FROM message;"

# launchd-spawned bash (after granting FDA to /bin/bash):
launchctl kickstart -k gui/$(id -u)/com.adelaida.imessage-export-vault
tail ~/Library/Logs/imessage-export-vault.launchd.log
# Expect: `export done rc=0`. `rc=1` with "unable to open database file" = FDA still missing.
```

## 2. Whisper

Reuses whatsapp-mcp's installation:
- Binary: `/opt/homebrew/bin/whisper-cli` (`brew install whisper-cpp`)
- Model: `~/.claude/whatsapp-mcp/models/ggml-large-v3.bin`

If whatsapp-mcp isn't installed, set `IMESSAGE_WHISPER_MODEL_PATH` and `IMESSAGE_WHISPER_BIN_PATH` directly.

## 3. Python deps

```bash
cd ~/.claude/imessage-mcp
uv sync
```

## 4. Register in vault `.mcp.json`

Add to the existing `mcpServers` block:

```json
"imessage": {
  "type": "stdio",
  "command": "/Users/YOU/.local/bin/uv",
  "args": [
    "--directory",
    "/Users/YOU/.claude/imessage-mcp",
    "run",
    "main.py"
  ],
  "alwaysLoad": true,
  "env": {
    "IMESSAGE_VAULT_OUT": "/Users/YOU/Documents/Vault/🤖 AI Chats/iMessage/",
    "IMESSAGE_VAULT_CRM_PATH": "/Users/YOU/Documents/Vault/👤 CRM/",
    "IMESSAGE_WHISPER_LANGUAGE": "en",
    "IMESSAGE_SCRUB_PROMPT_INJECTION": "true",
    "IMESSAGE_AUDIT_LOG": "true"
  }
}
```

Replace `/Users/YOU/` with your home directory; replace `Documents/Vault` with wherever your Obsidian vault lives.

Validate: `python3 -c "import json; json.load(open('/path/to/vault/.mcp.json'))"` — must print nothing (no exception).

Restart Claude Code. `claude mcp list` should show `imessage` connected.

## 5. Glue

Three artifacts ship outside this repo to mirror whatsapp-mcp:

- `~/.local/bin/imessage-export-vault.sh` — one-shot export wrapper
- `~/.claude/hooks/imessage-mcp-auto-export.py` — PostToolUse hook (30s rate limit)
- `~/Library/LaunchAgents/com.adelaida.imessage-export-vault.plist` — every 4h + RunAtLoad

Load the plist:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.adelaida.imessage-export-vault.plist
launchctl kickstart -k gui/$(id -u)/com.adelaida.imessage-export-vault
```

## 6. Smoke test

```bash
cd ~/.claude/imessage-mcp
uv run python3 -c "from chatdb import healthcheck; import json; print(json.dumps(healthcheck(), indent=2))"
uv run python3 -c "from export import export_to_vault; print(export_to_vault(min_messages=50))"
```

The first call must report `fda_granted: true` and a non-zero `chat_count`. The second must write at least one Markdown file under `🤖 AI Chats/iMessage/`.
