# Genesis Agent

**A self-hosted autonomous coding agent that actually runs things — and
remembers what it was doing.**

Genesis Agent lives on your machine, uses your API keys, and works through a
real terminal, a real browser, and a library of skills it can execute rather
than merely describe. When a session ends it writes down what was decided and
what comes next, so the following one does not start by asking you to explain
everything again.

```bash
pipx install git+https://github.com/me7ko-dev/genesis-agent
genesis setup        # asks for API keys, tests each one live
genesis              # start working
```

---

## Why another agent

Most coding agents forget you between sessions and hand work back as a to-do
list. Two things are different here.

**A skill library it can execute.** Skills are Markdown files with real Python
inside. The agent searches them semantically, loads the matching one, and runs
it through the sandbox — reusing working code instead of regenerating it. When
it solves something new, it saves that as a skill, so the library grows into
*your* library.

**Memory of the work, not of the commands.** Open threads with a concrete next
step, decisions with the reason behind them, preferences it learned from your
corrections. This is injected at the start of every session — including on your
phone. Crucially it is captured by a
mechanism, not by asking the model nicely to remember: an extraction pass runs
on session end and on context compaction, whether or not the model cooperated.

---

## Install

Requires Python 3.10+ and at least one API key (all providers have free tiers).
Not on PyPI yet, so install from git.

**Recommended — [pipx](https://pipx.pypa.io):** installs the `genesis` command
into its own environment and puts it on your PATH.

```bash
pipx install git+https://github.com/me7ko-dev/genesis-agent
```

On Debian/Ubuntu, get pipx first with `sudo apt install pipx && pipx ensurepath`.

**On Windows**, there is a native app — one line in PowerShell, no Python or
pipx needed, like Claude Code's installer:

```powershell
irm https://raw.githubusercontent.com/me7ko-dev/genesis-agent/main/scripts/install.ps1 | iex
```

It installs a self-contained `genesis.exe` (built and tested by CI on every
push to `main`), puts it on PATH, adds a Start menu entry and an uninstall entry,
and `/update` in the chat keeps it current.

**From your phone** (Android and iPhone): `genesis serve` shows a QR code;
scan it with the Genesis Remote app and use the same agent from the phone —
end-to-end encrypted, dangerous commands confirmed on the phone. See
[docs/MOBILE.md](docs/MOBILE.md).

Prefer your own Python? `scripts/install_windows.ps1` installs through pipx and first checks
the three things that actually break there — the Microsoft Store Python stub, a
stale PATH, and `bash.exe` resolving to the WSL launcher instead of Git Bash.
See [docs/WINDOWS.md](docs/WINDOWS.md), which also lists what differs on that
platform (no GUI; sandbox resource limits are POSIX-only).

<details>
<summary>Without pipx</summary>

A plain `pip install` fails on Debian, Ubuntu, Fedora and most current distros
with `error: externally-managed-environment` — the system Python is protected
([PEP 668](https://peps.python.org/pep-0668/)). Either of these works:

```bash
# a virtualenv — always works, nothing to install first
python3 -m venv ~/.venvs/genesis
~/.venvs/genesis/bin/pip install git+https://github.com/me7ko-dev/genesis-agent
~/.venvs/genesis/bin/genesis

# or install for your user only, opting out of the protection
pip install --user --break-system-packages git+https://github.com/me7ko-dev/genesis-agent
```

With `--user` the command lands in `~/.local/bin/genesis`; make sure that
directory is on your PATH.
</details>

`genesis setup` asks for each key, makes one real request to verify it, and
writes `~/.genesis/.env` with mode 600. Skip any provider you do not have —
one key is enough.

**Updating:** `genesis --version` names the exact installed commit, so
"is this the new version" has an answer. `genesis update` checks GitHub and
prints the command; `/update` inside the terminal chat does the same check
and, on confirmation, runs it for you — on the background, after you exit,
since pipx cannot overwrite the `genesis` executable while it is running.

Optional extras:

```bash
pip install "genesis-agent[browser]" && playwright install chromium   # web automation
pip install "genesis-agent[quality]"                                  # lint generated code with ruff
pip install "genesis-agent[signing]"                                  # RSA-signed skills
pip install "genesis-agent[all]"                                      # everything above, plus [premium]
```

For a local fallback that costs nothing, install [Ollama](https://ollama.com)
and pull a model. Genesis uses it only when every cloud provider has failed:

```bash
ollama pull qwen2.5-coder:3b   # last-resort brain
ollama pull bge-m3             # semantic skill search (Bulgarian too)
```

## Supported providers

Bring a key for any of these. Genesis walks the chain strongest-first and skips
whatever you have not configured.

| Provider | Free tier | Env var |
|---|---|---|
| HuggingFace Router | yes | `HF_TOKEN` |
| OpenRouter | yes | `OPENROUTER_API_KEY` |
| Ollama Cloud | yes | `OLLAMA_API_KEY` |
| Groq | yes | `GROQ_API_KEY` |
| NVIDIA NIM | yes | `NVIDIA_API_KEY` |
| Ollama (local) | free | `GENESIS_LOCAL_MODEL` |

Optional paid tier, off unless you ask for it — see [MAX mode](#max-mode):

| Provider | Env var |
|---|---|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |

**One key per provider.** Genesis does not rotate multiple accounts of the same
provider to multiply a quota — that violates most providers' terms of service.
Resilience comes from breadth instead. See [SECURITY.md](SECURITY.md).

## Commands

Genesis is terminal-only. Every command runs on the same engine
(`genesis_agent/agent_core.py`).

```bash
genesis                       # terminal chat
genesis mission "write a retry decorator with exponential backoff"
genesis fix ~/code/theirs "median() is wrong for even-length input"
```

## Steering it like Claude Code

The habits that make Claude Code pleasant to work with, in the Genesis chat:

| | What it does |
|---|---|
| `GENESIS.md` | Standing instructions for a project, read at the start of every session: how to test, what not to touch, the house style. `AGENTS.md` or `CLAUDE.md` is read when there is no `GENESIS.md`; `~/.genesis/GENESIS.md` holds yours for every project; `GENESIS.local.md` is personal and stays out of git. `@docs/style.md` inside one pulls that file in (only from the project, never a key). |
| `/init` | Genesis looks at the project and writes (or improves) its `GENESIS.md`. `/memory` shows which files are in force. |
| `/plan [task]` | Plan mode: it reads, searches and asks, and every tool that would change something is refused in code. `/plan` again approves the plan and it is carried out. |
| `/undo` | Puts back the files the last turn changed (created files are removed). Again → the turn before. Commands run with RUN_CMD are not tracked. |
| hooks | Your commands around its work: `~/.genesis/hooks.json` (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`; exit code 2 blocks or sends stderr back to the model — Claude Code's format is read too). A project's `.genesis/hooks.json` runs only after `/hooks trust`, and only while the file is unchanged. |
| `/name args` | Your own commands: `.genesis/commands/name.md` in the project or `~/.genesis/commands/`, `$ARGUMENTS` / `$1` replaced. `/commands` lists them. |
| background | `RUN_CMD` with `background: true` (`[RUN_BG: …]`) keeps a dev server running while it works; `BG_OUTPUT` / `BG_KILL`, `/bg` in the chat. |
| `/compact` | Summarise the history now instead of waiting for the threshold. |
| MCP | Tools from any MCP server (GitHub, databases, Slack, …), local (`command`) or hosted (`"type": "http", "url": …, "headers": {"Authorization": "Bearer ${TOKEN}"}`): `~/.genesis/mcp.json` in Claude Code's `.mcp.json` format (a project's `.mcp.json` only after `/mcp trust`). Read-only tools run at once; any other asks first, like a risky command (or list it in `autoApprove`). `/mcp` shows servers and tools. |
| `genesis -p "…"` | One turn without the chat, for scripts and CI: the answer alone on stdout (`--json` for one JSON object), piped stdin added to the task (`git diff \| genesis -p "review"`), `--plan` to change nothing, `--cwd` for the folder. Nobody can say yes, so anything risky is refused unless `--dangerously-allow`. |
| `WEB_FETCH` | Reads one page by its address as text (headings, lists, links, code) without a browser. Internal-network addresses are refused, also after a redirect; localhost is allowed for your dev server. |
| parallel reads | Several independent reads in one round (READ_FILE, SEARCH_CODE, GLOB, WEB_FETCH…) run at once. |
| `!command` | You run a command yourself, without the model (`!git status`, `!pytest -q`). The output shows at once and goes to the model with your next message, so "why does this fail?" needs no copying. |
| `@path` | Attaches a file (or a folder's listing) to your message: `why does @tests/test_api.py fail against @src/api/`. Works in `genesis -p` too. `.env` and keys are never attached. |
| `genesis -c` | Continues the last conversation in this folder (`/history` picks any older one). |
| `EXPLORE` | A read-only sub-agent: "where is the price computed and who calls it?" is searched and read in its own conversation, and only the answer (with `path:line`) comes back — the main conversation stays small. |

```json
{"hooks": {
  "PostToolUse": [{"matcher": "WRITE_FILE|EDIT_FILE", "command": "ruff format \"$GENESIS_FILE\""}],
  "Stop": [{"command": "python -m pytest -q || (echo 'tests fail' >&2; exit 2)"}]
}}
```

## Fixing an existing project

`genesis mission` writes new code, which it owns. `genesis fix` changes code
that is already someone's, already running, and where a wrong edit does not
fail loudly — it fails later, somewhere else, looking like a different bug.
Three things carry that difference, and all three are code rather than
instructions to the model:

- **A way back.** `genesis fix --checkpoint` snapshots the project before
  anything is touched, and `genesis fix --revert PATH` puts it all back. The
  snapshot is opt-in. Without it, a git project is undone with `git checkout .`,
  and a project that is not under git gets a warning that it has no way back.
- **The project's own test suite is the verdict.** Tests run before any change
  (so an already-red suite is not later blamed on the agent) and after every
  round of edits. "Fixed" means red → green; nothing else is reported as fixed.
- **A real diff at the end** — the bytes that changed, not the model's account
  of what it believes it did.

Edits are anchored (`EDIT_FILE`), not whole-file rewrites: the snippet must
already exist, must be unique, and if the result stops parsing, the file is
left untouched and the model gets the parse error back to try again.

```bash
genesis fix ~/code/theirs "median() is wrong for even-length input"
genesis fix ~/code/theirs "..." --test "npm test -- --run"   # if autodetection is wrong
genesis fix --revert ~/code/theirs                           # undo everything
```

### Coding mode — free

The default chain is a compromise between quality, speed and quota. That is the
right compromise for conversation and the wrong one for editing code you did
not write, so the stronger chain is a mode you turn on rather than the default:

```bash
genesis fix ~/code/theirs "..." --maxcoding    # or GENESIS_QUALITY=coding
/maxcoding                                     # toggle inside the terminal chat
```

It costs nothing — same free providers, a different selection and order. The
picks are by fitness for code, not by parameter count: two of them are
purpose-built coding-agent models that are *smaller* than the general giants
they sit next to, and every one is verified to support native tool-calling,
which the repair loop leans on. You pay in speed and quota instead of money —
these are the most contended free models — which is exactly why it is opt-in.

### MAX mode — optional, paid

`--max` puts a paid frontier model in front of everything else. **Without a paid
key it degrades to the free coding chain above**, so "give me your best" stays a
meaningful request whether or not you have a card on file:

```bash
pip install "genesis-agent[premium]"     # Anthropic's SDK; the rest need nothing
genesis fix ~/code/theirs "..." --max    # or: export GENESIS_QUALITY=max
```

⚠️ These keys **cost money per request**. Nothing is enabled by the key alone:
without `--max` / `GENESIS_QUALITY=max` the chain, the order and the spend are
exactly as before.

## Handing a project over

```bash
genesis export ~/code/invoices              # → invoices-20261007-1412.zip next to it
genesis export . -o client.zip --no-tests
```

In the chat: `/export` (or just "предай проекта"). The zip carries
`GENESIS_REPORT.md`, written from the project at that moment rather than from
the conversation: the files, how to run it (the README's own section, or worked
out from `requirements.txt` / `package.json` / `main.py`), its dependencies, the
test suite **run right then** with its result, the assumptions (the README's
"Assumptions" section plus the decisions remembered for that folder) and what is
still open. `.env`, `*.pem` and other secrets stay out of the archive and the
report names them, so the recipient knows to supply their own. Exit code 1 when
the tests fail.

## What it can do

- **Run commands** through a three-level safety gate (see below)
- **Read, write and surgically edit files**, and finish multi-step work across
  turns instead of stopping after the first tool call
- **Work on a codebase it did not write** — map a project, grep it, patch it,
  run its tests, undo everything (see [Fixing an existing project](#fixing-an-existing-project))
- **Drive a browser** — navigate, read, click, type — in an isolated profile
  with no access to your real cookies or passwords
- **Search and cross-check the web**: `RESEARCH` pulls several sources,
  extracts an answer from each independently, and reports disagreement honestly
  rather than picking one at random
- **Run and compose skills** from its library
- **Work autonomously** — a mission loop (plan → code → test → review → verify)
  that only saves a skill once it has actually passed a sandbox test

## Safety

Every command, file operation and browser action is classified `SAFE` (runs),
`CONFIRM` (asks you) or `BLOCKED` (never, in any mode). Password fields, card
fields and "place order" buttons are in the last category unconditionally.
Commands run with a minimal environment, so generated code cannot read your API
keys.

**Read [SECURITY.md](SECURITY.md) before running it unattended.** The short
version: the sandbox protects you from a model's mistakes, not from a
deliberate attacker. If the machine has anything valuable on it, use a
container.

## The skill library

Fourteen verified, dependency-free skills ship in `genesis_agent/skills/` — enough to see
the format and the reuse working. They are not the product; the mechanism is.
Point the agent at real work and the library becomes yours.

A skill is one Markdown file: metadata, a description, the code, and a
self-test. `verified: true` means that self-test really ran in the sandbox —
which is a much lower bar than "audited". Read a skill before trusting it with
anything that matters.

## Honest limitations

- **Free-tier models sometimes fabricate instead of calling a tool.** Native
  tool-calling makes it much rarer and usually self-correcting, but it still
  happens. Check summaries against actual tool output when it matters.
- **Prompt injection is not solved.** A page or file the agent reads can
  contain instructions aimed at the agent.
- **Local models are a fallback, not a peer.** A 3B model on a consumer GPU is
  there so the agent still answers when the cloud is down, not to match it.
- **Comments and some UI text are in Bulgarian.** The system prompts and
  documentation are English. Translating ~9000 lines of comments would cost a
  lot and buy little; PRs welcome if you disagree.

## Contributing

Issues and PRs welcome. Before opening a PR, run the unit test suite:

```bash
pip install -e .[dev]
pytest
```

It's isolated — no API keys or network needed, and it never touches your real
`~/.genesis` data. If you are changing behaviour the agent depends on
end-to-end, also run the integration test:

```bash
python scripts/e2e_integration_test.py
```

## License

MIT — see [LICENSE](LICENSE).
