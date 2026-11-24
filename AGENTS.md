# Instructions for coding agents

This file is for whatever coding agent a teammate uses (Codex, Cursor, Claude Code,
Aider, ...). Point your harness at this file if it does not pick it up on its own.

## What this project is

Vesper: a general AI coding agent, as a terminal app (`vesper`) and a desktop app. It reads,
searches, edits and runs commands, starting in the folder where the user opened it and
moving to other folders when asked. It asks before edits and commands, saves every
conversation in SQLite, and keeps a log of what it did. The agent loop is LangChain 1.x
`create_agent` (LangGraph underneath).

## Map

- `src/vesper/prompts.py`: every prompt sent to a model. The agent's system prompt is general on purpose: no topic, course or company in it. Project-specific guidance comes from the user's `VESPER.md` / `AGENTS.md` file, which `agent_system_prompt` appends.
- `src/vesper/workspace.py`: the tools (`list_files`, `read_file`, `glob`, `grep`, `change_directory`, `write_file`, `edit_file`, `run_command`, `update_tasks`, `web_fetch`, `web_search`). `Workspace.root` is the working directory and can move; paths are not confined to it. Every write, command or page fetch goes through `_gate`; in plan mode it refuses them.
- `src/vesper/permissions.py`: which commands run without asking: the read-only list, and the prefix rules an "always" answer saves in `config.json`.
- `src/vesper/webtools.py`: fetch a page as text and search the web (standard library only).
- `src/vesper/coder.py`: `Session`, the `create_agent` loop over those tools. Saves history to the store, logs events, summarizes long histories (`compact`) and trims what still does not fit (`fit_history`), repairs interrupted ones (`repair_history`), turns tool calls a small model wrote as text into real ones (`repair_tool_calls`), keeps file copies for `undo`, and goes back to before a user message (`rewind`, by its `vesper-turn-N` id). Saves after every step. `steer` adds a message the user sent mid-answer; a `before_model` middleware puts it in the turn before the next model call. Yields turn, token, reasoning, tool_draft, tool_start, tool_end, tasks, cwd, notice, steer, usage, unsteered and done events. `transcript()` turns saved messages into turns both front ends draw.
- `src/vesper/commands.py`: custom slash commands (Markdown files in `.vesper/commands`, `.claude/commands`, `~/.vesper/commands`) and skills (`SKILL.md` folders), whose names and descriptions `prompts.agent_system_prompt` lists.
- Small model: `small_provider` / `small_model` in `config.json`, `providers.get_small_llm`; `Session.small_llm` writes summaries (`compact`, falls back to the main model) and the first-turn title (`title` event).
- `src/vesper/fallback.py`: automatic model switching. `classify` sorts a failed model call into credits, rate, auth or gone; `auto_chain` builds the order (your model, paid models, free models ranked by `FAMILY_RANK`, then Ollama); `Switcher` remembers which models failed and until when. The `Session` guard middleware calls `_switch_after` and repeats the step on the next model. Config key `fallback`, `/fallback`, `GET/POST /api/fallback`.
- `src/vesper/checks.py`: the project's check command (`/check`, saved per folder in `config.json`); `Session` runs it from an `after_model` middleware when the agent finishes a turn that changed files, and hands failures back (event name `run_check`).
- `src/vesper/mcp.py`: the MCP client (stdio and Streamable HTTP, standard library only): reads `~/.vesper/mcp.json` and a project's `.mcp.json`, keeps one process-wide `Manager` of servers, and turns their tools into LangChain tools that go through `Workspace.call_mcp` and its approval.
- `src/vesper/documents.py`: turns Word, PowerPoint, Excel, OpenDocument, EPUB and RTF files into text for `read_file`, with the standard library only and size caps against zip bombs.
- `src/vesper/store.py`: `Store`, the SQLite database at `~/.vesper/vesper.db` (tables `sessions`, `messages`, `events`, `checkpoints`).
- `src/vesper/tui.py`: the `vesper` terminal app: session loop, slash commands, one-shot mode, `vesper sessions`, `vesper logs`. The agent runs in a worker thread; the main thread draws and asks.
- `src/vesper/ui.py`: how the terminal app looks (banner, tool lines, diffs, streaming Markdown, session and log tables). No model or file access. Colors come from `src/vesper/themes.py`; `ui.apply_theme` rebinds the color names (`ACCENT`, `MUTED`, ...) in `ui`, `tui` and `onboarding` at once, so code reads them at call time and never as default arguments. Config key `theme`, `/theme`.
- `src/vesper/picker.py`: the arrow-key list with type-to-search that `/resume` and Ctrl+R open (prompt_toolkit, inline).
- `src/vesper/telegram.py`: the Telegram bot (`/telegram`, `vesper telegram setup|on|off|status|run|approve`). Long polling with the standard library. State lives in `~/.vesper/telegram/` (`settings.json` written under a file lock by both the bot and the terminal; `state.json` with the offset and heartbeat). One poller at a time (`PollerLock`, flock). The offset is saved before each update is handled. HTML goes through `valid_html` before sending; lengths count UTF-16 units. Nothing in `Turn` may raise into the agent. Service: launchd on macOS, systemd user unit on Linux. Tests use a fake API that applies Telegram's own HTML and length rules.
- `src/vesper/onboarding.py`: `config.json`, the model and key pickers, the first-run and trust prompts.
- `src/vesper/providers.py`: picks the chat model, lists every model a key can use with prices, free models and tool support (`model_catalog`), and re-reads `~/.vesper/.env` when another Vesper process changes it, so keys are shared by the terminal and the desktop app. The only place that imports a provider package.
- `src/vesper/attachments.py`: files, folders and images attached to one message (`@path` in the terminal, `+`, drop or paste in the app). Images go to the model once and are replaced by a short note afterwards.
- `src/vesper/server.py`: FastAPI app on 127.0.0.1 with a per-launch token; streams answers as server-sent events. It runs a `Session` per chat in a worker thread and turns approvals into `approval` events answered via `POST /api/approve`. `GET /api/sessions` and `/api/sessions/{id}` serve the session database to the app; a chat request with `session_id` continues a saved conversation.
- `Session.changes()` / `revert_file()` in `coder.py` back the desktop review pane (`GET /api/chats/{id}/changes`, `POST .../changes/revert`); the pane is `apps/web/src/packages/ui-layout/Review.tsx`.
- `apps/web/`: the React UI (Vite + TypeScript). `npm run build` writes it to `src/vesper/web/` (generated, do not edit there). Model output goes through `renderMarkdown` (marked + DOMPurify); never insert it any other way.
- `apps/desktop/`: the Electron shell. It starts the engine (`--engine`), reads its `VESPER_READY` line and loads the UI. `preload.ts` exposes only native folder and file pickers and the path of a dropped file.
- `src/vesper/desktop.py`: engine entry point. `--engine` for Electron, `--serve` for UI development, `--smoke` tests a build, no flag opens a browser app window.
- `tests/`: offline tests with fake chat models.

## Commands

```bash
pip install -e ".[dev]"
pytest -q                 # must pass before every PR, needs no API key
npm --prefix apps/web test && npm --prefix apps/web run typecheck   # when you touch the UI
VESPER_FAKE_LLM=1 vesper  # try the terminal app with the offline demo model
```

## Rules

- Never commit `.env` or any API key. Keys go in `.env`, which git ignores.
- Use current APIs: `langchain.agents.create_agent` and its middleware. Do not use retired LangChain APIs.
- A new provider goes in `providers.py` only. Other modules must not change.
- A tool must never write a file or run a command without `_gate`. Dangerous commands and credential folders are `strict` (they ask in every mode); secret files are never read. Add a test for any new tool.
- Tokens are a budget. Tool descriptions stay one sentence, tool output is capped, and the fixed part of the system prompt comes before the per-session part so providers can cache it. Measure before adding to either.
- Keep prompts general. Nothing about a specific subject, school or company goes into `prompts.py`.
- Tests must not touch the real home folder. `conftest.py` points `HOME` and `VESPER_HOME` at temporary folders; use absolute paths under `tmp_path`.
- Keep tests offline. Use fake chat models (`FakeListChatModel`, `GenericFakeChatModel`), never a real API in `tests/`.
- One task per branch, one PR per task. Small PRs.
