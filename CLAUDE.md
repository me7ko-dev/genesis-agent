# Genesis Agent — instructions for coding agents

Read by Claude Code, and by Genesis itself (it reads `CLAUDE.md` when there is
no `GENESIS.md`). Short on purpose: `NEXT_STEPS.md` holds the plan and what is
already measured and decided — trust it, start from the task, do not re-audit
the whole repo.

## What this is
A terminal coding agent in Python: `genesis_terminal_agent.py` (chat),
`genesis_skills.py` (tools), `genesis_agent/` (sandbox, model chain in
`brain.py`, memory, skills, `genesis fix`, `genesis export`), `cloud/` (hosted
version), `mobile/` (Expo phone app — read `mobile/AGENTS.md` first; Expo APIs
change every SDK, check the versioned docs before touching them).

## Checks — all must pass before a commit
```bash
pip install -e .[dev,signing] && pip install mypy==2.3.1 ruff==0.16.8
python -m ruff check .
python -m mypy genesis_agent/ genesis_skills.py genesis_terminal_agent.py cloud/
python -m pytest -q
# mobile/: npm ci && npx tsc --noEmit && npx tsc -p tests --noEmit && npm test
```
CI runs Ubuntu 3.10/3.12/3.13 and Windows 3.12: no `tomllib` assumptions on
3.10, mind CRLF and `cmd.exe` quoting on Windows, no Cyrillic in shell
command lines a test runs.

## How work is done here
- **Measure, don't guess.** A behaviour change counts when a bench or a test
  shows it (`scripts/bench_all.py` on the laptop with real keys; here only
  fakes — no API keys in CI).
- **Reproduce first.** Every fix comes with a test that failed before it.
  Comments say *why*, with the date and what was measured (see existing code).
- Comments and user-facing text are in Bulgarian; identifiers in English.
- Tests never touch the operator's real state: `tests/conftest.py` redirects
  every persisted path and `~/.genesis`. A new module with state on disk gets
  added there.
- Safety lives in code, not prompts: the sandbox gate (`sandbox.py`) decides
  every command, plan mode refuses writes in `genesis_skills._before_tool`.
- When something is finished, update `NEXT_STEPS.md` briefly (strike what is
  done) instead of adding new prose.
