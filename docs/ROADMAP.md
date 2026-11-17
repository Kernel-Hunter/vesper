# Roadmap

Vesper aims to be the most complete free coding agent: the best ideas from every popular
harness in one tool that works with any model. This list is ranked by what users gain for
the work it takes. It was drawn up in November 2026 from the public docs of Claude Code,
Codex, Gemini CLI, OpenCode, Aider, Cline, Kilo Code, Cursor, Windsurf, Zed, Goose, Crush,
Amp, Warp, GitHub Copilot and Continue.

## Done

- **MCP servers** (0.10): `~/.vesper/mcp.json` and a project's `.mcp.json`, stdio and
  Streamable HTTP, approval for project servers, approval for tools that change things,
  and `mcp_find` / `mcp_call` instead of dozens of schemas when servers offer many tools.

## Next, in order

1. **Type while it works.** Messages sent during an answer are queued, or steer the
   current answer at its next step.
2. **Cost and context at a glance.** Dollars spent per chat from the price catalog, next to
   the context meter, with a warning near the limit.
3. **Custom commands and skills.** Markdown prompt files in `.vesper/commands`,
   `.claude/commands` and `.agents/skills`, loaded only when they are relevant, so folders
   made for other harnesses work here.
4. **Notifications.** A system notification and a dock badge when an answer finishes or
   waits for approval while the window is in the background.
5. **Background commands.** Start a dev server or a watcher, read its output later, stop it.
6. **Headless JSON mode.** `vesper -p "task" --json` streams events for scripts and CI.
7. **Test and lint loop.** A project test command that runs after edits; failures go back to
   the agent until they pass or it gives up.
8. **Small-model routing.** Titles, summaries and searches on a cheap model.
9. **Git safety net.** A commit per turn on request, and a worktree per parallel chat.
10. **Diff review pane.** Every change of a turn file by file, accept or reject per hunk, and
    comments on a line that go back to the agent.
11. **Diagnostics after edits.** Type errors from the language server (pyright, tsserver,
    gopls) go back to the agent right away.
12. **Subagents.** A child agent with its own context for searches and side tasks.

Later: hooks, memory across sessions, a browser tool, an ACP server for editors, session
export and sharing, a checkpoint timeline, symbol and URL mentions, keybindings and a vim
mode, and scheduled recipes.
