# Upgrade plan: Ruff pre-validation, browser bbox, graph memory

Written 2026-07-29 at end of a session that ran out of quota — this is the
implementation prompt for whoever (human or Claude) picks this up next.
**Grounded against the actual code, not assumptions** — the original ask
(pasted below the plan) got some of the codebase wrong, corrected inline.

## Status check against the real repo (do this again if time has passed)

- `ruff` is **not installed and not a dependency yet** (`which ruff` → nothing,
  no mention in `pyproject.toml`/`requirements.txt`). Feature 1 is genuinely
  new.
- `genesis_agent/browser.py` (229 lines) **already does most of Feature 2** —
  numbered interactive elements, markdown-formatted, exactly the "Browser
  Use" pattern the ask describes. Don't rebuild it. The one real gap:
  `_SCAN_JS` (browser.py:34-53) doesn't capture bounding boxes, only
  visibility. Small, scoped addition, not a refactor.
- `genesis_agent/workspace_memory.py` (434 lines) already has the
  session-end extraction pass (`auto_capture()`, line ~304) with a flat
  `threads`/`decisions`/`preferences` schema. Feature 3 is a genuine new
  layer, additive alongside this, not a replacement — `auto_capture()` and
  `briefing()` work and are called from real hook points
  (`agent_core.run_tool_loop`'s compaction, GUI's `on_close`); don't risk
  them.

## Feature 1 — `validate_code_with_ruff`

New file `genesis_agent/code_validate.py`:

```python
from __future__ import annotations
import json
import shutil
import subprocess

_RUFF_TIMEOUT = 10

def _ruff_available() -> bool:
    return shutil.which("ruff") is not None

def validate_code_with_ruff(code: str) -> tuple[bool, str]:
    """Lint `code` with ruff before it ever reaches the sandbox. Fail-open:
    if ruff isn't installed, returns (True, "") rather than blocking the
    pipeline — same best-effort convention as provider_stats/workspace_memory
    (never let an optional quality check become a hard dependency).
    Auto-fixes what ruff can (--fix), returns the FIXED code as the second
    element when there were no unfixable errors; returns the ORIGINAL code
    plus a formatted error list when something needs the LLM's attention."""
    if not _ruff_available():
        return True, ""
    try:
        fixed = subprocess.run(
            ["ruff", "check", "--fix", "--exit-zero", "--stdin-filename", "gen.py", "-"],
            input=code, capture_output=True, text=True, timeout=_RUFF_TIMEOUT,
        )
        result = subprocess.run(
            ["ruff", "check", "--output-format", "json", "--stdin-filename", "gen.py", "-"],
            input=fixed.stdout or code, capture_output=True, text=True, timeout=_RUFF_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return True, ""  # never block the loop on a tooling hiccup
    if result.returncode == 0:
        return True, fixed.stdout or code
    try:
        errors = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False, result.stdout[:1000]
    lines = [f"  L{e['location']['row']}: {e['code']} {e['message']}" for e in errors[:15]]
    return False, "Ruff found issues:\n" + "\n".join(lines)
```

Notes for the implementer:
- `ruff check --fix --exit-zero` first (auto-fixes what it safely can, never
  fails the process), THEN a clean `ruff check` to see what's left — two
  subprocess calls, but ruff is a Rust binary, sub-10ms typically, not worth
  optimizing to one call.
- `--select` defaults to ruff's own sane defaults; don't turn on the pedantic
  rule sets (docstrings, import sorting) — this is gating *generated* code,
  not enforcing house style. If false positives show up in practice (e.g.
  unused imports the LLM left in a WIP file on purpose), add a project
  `ruff.toml`/`[tool.ruff]` in `pyproject.toml` with a narrow `select`.

Integration points (both, not either/or):
1. **`autonomous_loop.py`/`orchestrator.py`** (the mission loop) — after the
   LLM round produces `reply.code`, call `validate_code_with_ruff` BEFORE
   `sandbox.run_python`/the verifier. On failure, feed the error string back
   as the next round's context (same shape as an existing execution-failure
   retry) instead of burning a sandbox round on code that can't even parse.
2. **`genesis_skills.py`**'s `WRITE_FILE` dispatch — when the target path
   ends in `.py`, run validation and include the result in the tool-result
   text (visible to the model in the next round), the same way
   `agent_core._diff_for_write` already annotates writes with a diff.

`pyproject.toml`: add `ruff` under `[project.optional-dependencies]` (e.g. a
new `dev` or `quality` extra), not a hard dependency — matches the fail-open
design above and keeps the base install light (a stated project value, see
README's pipx recommendation).

## Feature 2 — bounding boxes in the browser tool

`browser.py`'s `_SCAN_JS` (line ~34): add a `rect` capture per element:

```js
const r = el.getBoundingClientRect();
out.push({i, tag, type, text, name, x: Math.round(r.x), y: Math.round(r.y),
          w: Math.round(r.width), h: Math.round(r.height)});
```

`_format_elements()` (line ~100): **keep the default output exactly as
today** (no coordinates in the text tree the model reads every turn — this
is the "minimize token clutter" half of the original ask, and browser.py's
docstring already states that goal). Coordinates land in `_last_elements`
for internal use only (e.g. a future `click_at(x, y)` fallback for elements
JS `querySelector` can't reliably re-find, or overlap detection between two
elements at the same visible position) — don't print them per-line by
default. If a future need shows up for the model to *see* positions, add it
as an opt-in second formatter (`_format_elements(elements, with_coords=True)`)
rather than changing the default and taxing every browser turn's token cost.

## Feature 3 — `compact_and_graph_memory`

New file `genesis_agent/knowledge_graph.py`, same conventions as
`workspace_memory.py` (best-effort, never raises, `DATA_DIR`-relative
storage) but its own store — this is additive alongside `auto_capture()`,
not a replacement:

```python
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any
from genesis_agent.config import DATA_DIR

GRAPH_PATH = DATA_DIR / "knowledge_graph.json"

# Schema (JSON, not SQLite — the ask specifically wants JSON/Markdown output,
# and the graph is small enough that a single-file read-modify-write is
# simpler than a join-heavy schema for what's fundamentally a few hundred
# entities across a project's lifetime):
# {
#   "entities":  {"<name>": {"type": str, "first_seen": iso, "last_seen": iso}},
#   "relations": [{"source": str, "relation": str, "target": str, "at": iso}],
#   "states":    {"<entity>": {"status": str, "updated_at": iso}}
# }

def _load() -> dict[str, Any]:
    if not GRAPH_PATH.exists():
        return {"entities": {}, "relations": [], "states": {}}
    try:
        return json.loads(GRAPH_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"entities": {}, "relations": [], "states": {}}

def _save(graph: dict[str, Any]) -> None:
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_PATH.write_text(json.dumps(graph, ensure_ascii=False, indent=2))

def compact_and_graph_memory(session_logs: str) -> dict[str, Any]:
    """LLM extraction pass, SAME pattern as workspace_memory.auto_capture()
    (reuses Brain, same best-effort try/except-and-return-empty shape) —
    call this ALONGSIDE auto_capture at its existing hook points
    (agent_core.run_tool_loop's compaction call, GUI's on_close), not instead
    of it. Extracts entities/relations/active-states, merges into the
    persisted graph (dedup entities case-insensitively — same lesson as
    workspace_memory.add_thread's dedup, an LLM will re-mention "AuthToken"
    and "auth token" as if new every time otherwise), returns the merged
    graph dict for the caller to also render into next-session briefing."""
    from genesis_agent.brain import Brain
    prompt = (
        "Извлечи от разговора структуриран граф: entities (name, type), "
        "relations (source, relation_type ГЛАГОЛ_UPPERCASE, target), "
        "states (entity, status). Само JSON, схема:\n"
        '{"entities":[{"name":..,"type":..}],'
        '"relations":[{"source":..,"relation":..,"target":..}],'
        '"states":[{"entity":..,"status":..}]}\n\n'
        f"Разговор:\n{session_logs[-6000:]}"
    )
    try:
        reply = Brain(min_size_b=120).complete([{"role": "user", "content": prompt}])
        extracted = json.loads(reply.raw_text.strip().strip("`").removeprefix("json"))
    except Exception:
        return _load()  # fail-open, same as auto_capture

    graph = _load()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for e in extracted.get("entities", []):
        key = str(e.get("name", "")).strip()
        if not key:
            continue
        existing = graph["entities"].get(key.lower())
        graph["entities"][key.lower()] = {
            "name": key, "type": e.get("type", ""),
            "first_seen": (existing or {}).get("first_seen", now), "last_seen": now,
        }
    for r in extracted.get("relations", []):
        graph["relations"].append({**r, "at": now})
    graph["relations"] = graph["relations"][-200:]  # cap growth, same spirit as threads/decisions limits
    for s in extracted.get("states", []):
        ent = str(s.get("entity", "")).strip()
        if ent:
            graph["states"][ent.lower()] = {"status": s.get("status", ""), "updated_at": now}

    _save(graph)
    return graph

def graph_briefing(max_relations: int = 12) -> str:
    """Compact text form for system-prompt injection — same role as
    workspace_memory.briefing(), and MUST stay similarly small (~150-250
    tokens), not a JSON dump. Call alongside briefing() at session start."""
    graph = _load()
    lines = []
    for r in graph["relations"][-max_relations:]:
        lines.append(f"- {r['source']} {r['relation']} {r['target']}")
    for name, s in graph["states"].items():
        if s.get("status"):
            lines.append(f"- {name}: {s['status']}")
    return "\n".join(lines) if lines else ""
```

Integration:
- `agent_core.py`'s `run_tool_loop`, where `core.wm.auto_capture(pre_compact)`
  is already called on compaction — add a sibling call to
  `knowledge_graph.compact_and_graph_memory(...)` right next to it (same
  trigger, same input).
- `Core.__init__` (agent_core.py, where `self.briefing` is built from
  `core.wm.briefing()`) — append `knowledge_graph.graph_briefing()`'s output
  if non-empty, same pattern as how `briefing` is already assembled.
- Test the *dedup* path deliberately before trusting it — this is exactly the
  kind of thing where a naive first pass silently duplicates "Project X" and
  "project x" as two entities forever (see workspace_memory's own dedup
  lesson, `add_thread`, `2a87e9b`'s slug-collision bug). Don't skip that
  check just because the JSON round-trips.

## Verification checklist for whoever implements this

1. `python3 -m mypy genesis_agent/code_validate.py genesis_agent/knowledge_graph.py`
2. `pip install ruff` locally, confirm `validate_code_with_ruff` catches an
   obvious syntax error AND a real lint issue (unused import), confirm it
   returns `(True, "")` when `ruff` is uninstalled (uninstall/rename the
   binary temporarily to check the fail-open path for real, don't just read
   the code and assume).
2. Browser bbox: navigate to a real page, confirm `_last_elements` entries
   carry `x/y/w/h`, confirm the DEFAULT `_format_elements()` output is
   byte-identical to before (no token-cost regression).
3. Knowledge graph: run `compact_and_graph_memory` against a real
   conversation with a repeated entity mentioned two different ways, confirm
   it dedupes into one entry, not two. Confirm `graph_briefing()` stays under
   ~250 tokens on a graph with 50+ relations (the cap at line
   `graph["relations"][-200:]` bounds file growth, but `max_relations` bounds
   what's actually injected).
4. `scripts/e2e_integration_test.py` — still 6/6 after both new modules are
   wired into the two call sites above.
5. Commit + push to GitHub (this repo's convention — see git log), CI green.

---

## Original ask (as given, for reference)

Act as an expert Senior Python AI Engineer and Architect. I am developing
"Genesis Agent" — a self-hosted autonomous coding agent. [...] three core
features inspired by elite open-source libraries: Ruff, Browser Use, and
Microsoft's GraphRAG. [Full text preserved in the conversation this plan was
written from — not duplicated here to keep this file focused on the
corrected, codebase-grounded plan above.]
