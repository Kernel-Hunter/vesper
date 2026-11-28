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
- **Telegram, rebuilt** (0.23.0): guided setup inside Vesper (paste the token, tap Start, confirm), a login service, one poller at a time, no repeated or late tasks after a restart, a health view in `/telegram`, photos and files, typed yes/no approvals, and every message type checked against Telegram's own parser.
- **Windows fix** (0.23.1): the one-bot-at-a-time lock now works on Windows too, so two bots on one token can no longer fight over messages.
- **Telegram in the terminal** (0.24.0): what the bot does on Telegram shows live in any open Vesper terminal, above the prompt, and in `vesper telegram watch`. The status names whoever is paired.
- **Bot folder kept** (0.24.1): the bot's folder is saved once, so restarting it from another folder no longer moves it. `/cd` in the chat or a new setup changes it.
- **Readable on any background** (0.24.2): Vesper asks the terminal for its background color, and a theme made for the other kind (dark text on a black terminal) is swapped for one that fits, with a one-line note. `/theme` lists only themes that suit the window. Dim text is brighter in every theme.
- **Git safety net, part one** (0.25.0): `/git auto on` commits each turn's changes (only Vesper's files, the user's identity and hooks, never during a merge or on a detached HEAD); `/undo` takes the commit back. A conversation deleted in another window stops saving instead of printing errors.
- **Daylight on dark terminals** (0.25.1): Daylight keeps its exact colors in a dark terminal; only the text and diff backgrounds change so they stay readable. `/theme daylight` fits either kind of window.
- **Subagents** (0.28.0): a `subagent` tool hands a self-contained question to a helper with a fresh context and read-only tools (files and the web); only its findings come back. Up to four run at once, their steps show live, their tokens count toward the answer, and stopping the answer stops them.
- **Memory across sessions** (0.30.0): a `remember` tool keeps notes about the project or the user in `~/.vesper/memory/` (asking first, like an edit); every conversation starts with them, and `/memory` lists and forgets them.
- **Hooks** (0.29.0): `PreToolUse`, `PostToolUse`, `UserPromptSubmit` and `Stop` commands in `~/.vesper/hooks.json` or a project's `.vesper/hooks.json`, in Claude Code's format with its tool names accepted. Exit code 2 blocks or sends feedback; a project's file runs only once approved with `/hooks approve`, and the agent must ask to write either file. The installers no longer use the GitHub API, which refused them on shared networks.
- **Helpers never wait** (0.28.1): a helper that would need the user's approval to fetch a page reports the address instead, since its steps have no approval card in the app; helper progress shows under the helper's step in the desktop app.
- **Diagnostics after edits** (0.27.0): after `write_file` or `edit_file`, the errors the edit introduced come back from the project's language server (basedpyright/pyright, TypeScript 7 or typescript-language-server, gopls, clangd), with errors that were already there left out. Servers already installed only; `/diagnostics` shows and toggles it.
- **Git safety net, part two** (0.26.0): `/git worktree` gives a chat its own git worktree on a `vesper/` branch, so parallel chats in one repository never edit the same files; `/git merge` brings it back only when that is safe, `/git discard` asks before throwing it away. The desktop app has the same buttons, and shows which commit each answer made.
- **Telegram and easy history** (0.22.0): `vesper telegram` puts Vesper in a Telegram chat, with every step, diff and approval shown live; in the terminal, Ctrl+R or `/resume` opens a searchable list of past conversations, recent ones show at start, and resuming replays the conversation on screen.
- **Terminal polish** (0.21.0): solid lit wordmark with a few stars, your messages on a tinted band, a star marking each reply, keys drawn as keycaps, `/help` grouped by topic.
- **Terminal redesign** (0.20.0): a gradient wordmark, eight themes (`/theme`), a status bar with a context meter and git branch, diffs with line numbers and tinted lines, a twinkling spinner with a moving shimmer, and code blocks marked by a thin bar.
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

1. **ACP server.** Vesper as the agent inside editors that speak the Agent Client Protocol.
2. **Session export.** A conversation as a Markdown or HTML file to share or keep.
3. **Checkpoint timeline.** Every turn's file state, listed, with a jump back to any of them.

Later: a browser tool, symbol and URL mentions, keybindings and a vim mode, and scheduled
recipes.
