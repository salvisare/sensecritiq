# SenseCritiq MCP Server

SenseCritiq turns raw research into actionable insights — automatically.

Upload an interview recording, transcript, or survey and Claude will extract themes, surface verbatim quotes, and generate structured findings in minutes. No more manual affinity mapping. No more hours spent tagging transcripts. Just clear, cited insights ready to share with your team.

Built for UX researchers, product teams, and anyone who talks to users.

## Setup

1. Get your API key at [sensecritiq.com](https://sensecritiq.com)
2. Add to your Claude Desktop `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "sensecritiq": {
      "command": "uvx",
      "args": ["sensecritiq-mcp"],
      "env": {
        "SCQ_API_KEY": "scq_live_your_key_here"
      }
    }
  }
}
```

## Tools

- `upload_research_file` — Upload audio, video, or transcript for processing
- `get_session_status` — Poll processing status (queued → processing → ready)
- `get_synthesis` — Get themes, findings, and quote count
- `get_quotes` — Get verbatim quotes, optionally filtered by theme
- `search_research` — Semantic search across all sessions
- `list_sessions` — List all research sessions
- `generate_report` — Export as Markdown or PDF

## Supported file types

mp3, mp4, wav, m4a, txt, pdf, docx
