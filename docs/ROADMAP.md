# Roadmap

Vesper aims to be the most complete free coding agent: the best ideas from every popular
harness in one tool that works with any model. This list is ranked by what users gain for
the work it takes. It was drawn up in November 2026 from the public docs of Claude Code,
Codex, Gemini CLI, OpenCode, Aider, Cline, Kilo Code, Cursor, Windsurf, Zed, Goose, Crush,
Amp, Warp, GitHub Copilot and Continue.

## Done

- **MCP servers** (0.10.1): `~/.vesper/mcp.json` and a project's `.mcp.json`, stdio and
  Streamable HTTP, approval for project servers, approval for tools that change things,
  and `mcp_find` / `mcp_call` instead of dozens of schemas when servers offer many tools.
- **Type while it works** (0.10.1) and **steering** (0.13.0): a message sent during an answer
  joins it before the agent's next step; one that arrives too late goes out when it ends.
- **Test and lint loop** (0.14.0): `/check pytest -q` runs after the agent's edits; failures go
  back to it until they pass or three tries are used.
- **Automatic model switching** (0.19.0): when a model runs out of credits or hits a limit, the turn goes on with the next one: paid models first, then free models best first, then a local one (`/fallback`).
- **Pick up where you left off** (0.18.0): the app reopens the last conversation and unsent drafts, asks before quitting mid-answer, offers Continue for answers cut off by a quit or crash; incremental saving and windowed drawing for very long sessions.
- **Interface polish and studio themes** (0.17.0): depth, motion and quieter sidebar; Dusk, Graphite and Carbon themes; text size, density, width, font and message style settings.
- **Small model** (0.16.0): titles and summaries on a cheaper model you pick (`/small`).
- **Review pane** (0.15.0): every file the chat changed with its diff against the first copy,
  comments on a line that go into the next message, and revert per file.
- **Cost of each answer** (0.10.1) at list prices, and the chat's total.
- **Custom commands and skills** (0.10.1), in the `.claude/commands` and `SKILL.md` formats.
- **Notifications** (0.10.1) when an answer ends or waits for approval in the background.
- **JSON mode** (0.11.0): `vesper --json "task"` streams events for scripts and CI.
- **Quiet conversation view** (0.11.0): tool calls in a row fold into one line ("Read 3 files,
  edited a file +12 −3") that opens to show each step, a slimmer message box, and lines
  added and removed per file in the Environment panel.
- **New start screen and answers** (0.12.0): quick starts on the start screen, answers without an avatar column, and code blocks with syntax colors, the language and
  a Copy button.
- **Background commands** (0.10.1): dev servers and watchers keep running; the agent reads
  their new output and stops them.

## Next, in order

1. **Git safety net.** A commit per turn on request, and a worktree per parallel chat.
2. **Diagnostics after edits.** Type errors from the language server (pyright, tsserver,
    gopls) go back to the agent right away.
3. **Subagents.** A child agent with its own context for searches and side tasks.

Later: hooks, memory across sessions, a browser tool, an ACP server for editors, session
export and sharing, a checkpoint timeline, symbol and URL mentions, keybindings and a vim
mode, and scheduled recipes.
