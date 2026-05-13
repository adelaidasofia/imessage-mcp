# imessage-mcp

macOS-only MCP server that exposes iMessage history to Claude and exports per-chat conversations into an Obsidian vault. Mirrors the whatsapp-mcp pattern: same hook, same launchd cadence, same vault shape.

## Architecture

- Direct read of `~/Library/Messages/chat.db` (SQLite, opened `mode=ro&immutable=1`). No bridge daemon.
- Voice-note transcription via Whisper local-cpp (reuses the model already installed for whatsapp-mcp).
- FTS5 incremental search index at `~/.claude/imessage-mcp/search.db`.
- Vault export per chat into `🤖 AI Chats/iMessage/<Contact>.md` and `🤖 AI Chats/iMessage/Groups/<Group>.md`.
- Send pathway via AppleScript with draft+confirm pattern (no auto-send).

## Tool surface

**Read:** healthcheck, list_chats, list_messages, get_chat, search_contacts, search_messages, get_message_context, get_unread, get_thread, list_attachments, mark_chat_read, export_to_vault.

**Write:** send_message (drafts an AppleScript send), confirm_send (commits).

## Install

Open Claude Code, paste:

    /plugin marketplace add adelaidasofia/imessage-mcp
    /plugin install imessage-mcp@imessage-mcp

Then grant Full Disk Access to Claude.app (required for `chat.db` read) and restart Claude Code. Full setup details and env vars in [SETUP.md](SETUP.md).

<details><summary>Legacy install (manual <code>.mcp.json</code> registration)</summary>

See [SETUP.md](SETUP.md). The short version: grant Full Disk Access to Claude.app, run `uv sync`, register in vault `.mcp.json`, restart Claude Code.

</details>

## Vault export shape

```yaml
---
type: imessage-chat
contact: "Jane Doe"
phone: "+15555550123"
service: iMessage
chat_guid: "iMessage;-;+15555550123"
message_count: 412
first_message: 2024-08-12
last_message: 2026-05-06
last_sync: 2026-05-07
---
```

Body sections per date with reactions, edits, replies, voice-note transcripts, and attachment wikilinks.

## Related MCPs

Same author, same architecture pattern (FastMCP, draft+confirm on writes where applicable, vault auto-export, MIT):

- [slack-mcp](https://github.com/adelaidasofia/slack-mcp) - multi-workspace Slack
- [whatsapp-mcp](https://github.com/adelaidasofia/whatsapp-mcp) - WhatsApp via whatsmeow
- [google-workspace-mcp](https://github.com/adelaidasofia/google-workspace-mcp) - Gmail / Calendar / Drive / Docs / Sheets
- [apollo-mcp](https://github.com/adelaidasofia/apollo-mcp) - Apollo.io CRM + sequences
- [substack-mcp](https://github.com/adelaidasofia/substack-mcp) - Substack writing + analytics
- [luma-mcp](https://github.com/adelaidasofia/luma-mcp) - lu.ma events
- [parse-mcp](https://github.com/adelaidasofia/parse-mcp) - markitdown / Docling / LlamaParse router
- [rescuetime-mcp](https://github.com/adelaidasofia/rescuetime-mcp) - RescueTime productivity data
- [graph-query-mcp](https://github.com/adelaidasofia/graph-query-mcp) - vault knowledge graph queries
- [investor-relations-mcp](https://github.com/adelaidasofia/investor-relations-mcp) - seed-raise pipeline tracker
- [vault-sync-mcp](https://github.com/adelaidasofia/vault-sync-mcp) - bidirectional vault sync

## License

MIT.

---

Built by Adelaida Diaz-Roa. Full install or team version at diazroa.com.
