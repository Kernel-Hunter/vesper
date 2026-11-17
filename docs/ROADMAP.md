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
- **Type while it works** (0.10.1): messages sent during an answer wait above the message
  box and go out one by one when it ends.
- **Cost of each answer** (0.10.1) at list prices, and the chat's total.
- **Custom commands and skills** (0.10.1), in the `.claude/commands` and `SKILL.md` formats.
- **Notifications** (0.10.1) when an answer ends or waits for approval in the background.
- **JSON mode** (0.11.0): `vesper --json "task"` streams events for scripts and CI.
- **Quiet conversation view** (0.11.0): tool calls in a row fold into one line ("Read 3 files,
  edited a file +12 −3") that opens to show each step, a slimmer message box, and lines
  added and removed per file in the Environment panel.
- **Background commands** (0.10.1): dev servers and watchers keep running; the agent reads
  their new output and stops them.

## Next, in order

1. **Steering.** A message sent during an answer can also join it at its next step.
2. **Test and lint loop.** A project test command that runs after edits; failures go back to
   the agent until they pass or it gives up.
3. **Small-model routing.** Titles, summaries and searches on a cheap model.
4. **Git safety net.** A commit per turn on request, and a worktree per parallel chat.
5. **Diff review pane.** Every change of a turn file by file, accept or reject per hunk, and
    comments on a line that go back to the agent.
6. **Diagnostics after edits.** Type errors from the language server (pyright, tsserver,
    gopls) go back to the agent right away.
7. **Subagents.** A child agent with its own context for searches and side tasks.

Later: hooks, memory across sessions, a browser tool, an ACP server for editors, session
export and sharing, a checkpoint timeline, symbol and URL mentions, keybindings and a vim
mode, and scheduled recipes.
