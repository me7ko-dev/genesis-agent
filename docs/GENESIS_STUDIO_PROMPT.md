# Genesis Studio — desktop app spec (2026-07-28)

## Why

The maintainer wants a customer-facing desktop application for Genesis Agent with the
same *feel* as Claude Code: a coding assistant that lives in your project,
not just a chatbot in a browser tab. A follow-up mobile app is planned later,
reusing the same core.

## Decisions (asked, not assumed)

1. **Desktop GUI, not terminal CLI.** Genesis Agent already ships a terminal
   REPL (`genesis_terminal_agent.py`) with full tool-loop/model-switching/
   memory — that's the "code environment" engine. This spec is about giving
   it a face customers will actually want to use, not rebuilding the engine.
2. **Polished MVP, not full Claude Code parity.** No subagents, no MCP, no
   hooks, no plan-mode-as-a-product-feature. Real streaming-*feeling*
   responses, visible model switching, a code/workspace pane, and diffs for
   file writes — the things that make an agent *feel* like it's working in
   your code, not just talking about it.
3. **Built on `genesis-agent`, not a new repo.** Extends
   `genesis_agent/gui/genesis_gui.py` (GTK4/libadwaita) and the shared
   `genesis_agent/agent_core.py` (used by GTK *and* Jarvis — changes here
   must not break the voice frontend).

## Gap vs. Claude Code (what's missing, ranked)

1. **No code/workspace pane.** Pure chat with collapsible tool-output blobs.
   Nothing on screen says "this agent is looking at your files."
2. **No visible model control.** `Core.complete()` builds a fresh `Brain()`
   every call with no way to pin a model from the UI — the terminal has
   `/model`, the GUI has nothing.
3. **No diffs.** `WRITE_FILE` returns `"✓ записани N символа"` — a file
   changed and the user can't see what changed without opening it elsewhere.
4. **No streaming feel.** Full response lands as one paste. Not real SSE
   streaming (Brain's HTTP layer isn't built for it, and rebuilding it is out
   of MVP scope) — a typewriter reveal of the already-received text closes
   most of the perceived gap for far less risk.
5. **No slash commands in the input box.** `/tasks`/`/budget`/`/clear` only
   exist as menu clicks; Claude Code users type them.

## Plan

### `agent_core.py` (shared — GTK *and* Jarvis)
- `Core.pin_model: tuple[str, str] | None`, `Core.available_models()` (reads
  the chain from a throwaway `Brain()`, dedup by provider+model).
- `Core.complete()` passes `pin_model=self.pin_model` through.
- `run_tool_loop`: for `WRITE_FILE` calls, read the file's content *before*
  dispatch, diff it against the new content after, pass it through
  `on_tool_result(name, result, extra=...)`. New `extra` param is optional
  (default `None`) so Jarvis's existing 2-arg callback keeps working
  unchanged — only the GTK app needs to opt in.

### `genesis_gui.py`
- Model-switcher `MenuButton` in the header, backed by `available_models()`.
- `/model`, `/help`, `/clear`, `/tasks`, `/budget`, `/done`, `/drop` parsed
  from the input box before it hits the LLM.
- `ToolWidget` renders a real +/- colored diff when `extra["diff"]` is set.
- Collapsible workspace file panel (`Gtk.Paned`): flat recursive listing
  (ignoring `.git`/`__pycache__`/etc.), click a file → read-only preview.
- Typewriter reveal for assistant text via `GLib.timeout_add`.
- Header always shows the active model, not just "готов"/"мисли…".

### Explicitly out of scope this pass
- True token-level SSE streaming (needs a Brain HTTP rework).
- Cross-platform packaging (GTK4/libadwaita is Linux-only; Windows/macOS
  customers need a different toolkit — a real decision for the *next* pass,
  not silently absorbed into this one).
- Mobile app (separate project, reuses `agent_core.py`/`brain.py` — no GUI
  code is shared with a mobile client regardless of what we build here).

## Verification

- `python3 -m mypy genesis_agent/ genesis_skills.py genesis_terminal_agent.py`
  stays at 0 errors.
- `python3 -m py_compile` on every touched file.
- `scripts/e2e_integration_test.py` stays 6/6 (agent_core changes must not
  touch the sandbox/memory paths it exercises).
- Real launch on the live desktop (`DISPLAY=:0` is available in this
  session) — not just static analysis. Screenshot the running app,
  exercise: send a message, switch a model, trigger a file write and see
  the diff, open the workspace panel, run a slash command.
