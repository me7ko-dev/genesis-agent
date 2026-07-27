# Genesis Agent

**A self-hosted autonomous coding agent that actually runs things — and
remembers what it was doing.**

Genesis Agent lives on your machine, uses your API keys, and works through a
real terminal, a real browser, and a library of skills it can execute rather
than merely describe. When a session ends it writes down what was decided and
what comes next, so the following one does not start by asking you to explain
everything again.

```bash
pip install git+https://github.com/me7ko-dev/genesis-agent
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
phone, if you use the Discord frontend. Crucially it is captured by a
mechanism, not by asking the model nicely to remember: an extraction pass runs
on session end and on context compaction, whether or not the model cooperated.

---

## Install

Requires Python 3.10+ and at least one API key (all providers have free tiers).

```bash
pip install git+https://github.com/me7ko-dev/genesis-agent
genesis setup
```

> Not on PyPI yet, so install from git for now. `pip install genesis-agent`
> will work once the first release is published there.

`genesis setup` asks for each key, makes one real request to verify it, and
writes `~/.genesis/.env` with mode 600. Skip any provider you do not have —
one key is enough.

Optional extras:

```bash
pip install "genesis-agent[browser]" && playwright install chromium   # web automation
pip install "genesis-agent[discord]"                                  # phone access
pip install "genesis-agent[voice]"                                    # speak to it
```

For a local fallback that costs nothing, install [Ollama](https://ollama.com)
and pull a model. Genesis uses it only when every cloud provider has failed:

```bash
ollama pull qwen2.5-coder:3b   # last-resort brain
ollama pull nomic-embed-text   # semantic skill search
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
| Cohere | trial | `COHERE_API_KEY` |
| Ollama (local) | free | `GENESIS_LOCAL_MODEL` |

**One key per provider.** Genesis does not rotate multiple accounts of the same
provider to multiply a quota — that violates most providers' terms of service.
Resilience comes from breadth instead. See [SECURITY.md](SECURITY.md).

## Frontends, one core

Every frontend calls the same engine (`genesis_agent/agent_core.py`), so tools,
skills, sandbox and memory behave identically in all of them.

```bash
genesis                       # terminal chat
genesis gui                   # GTK window
genesis voice                 # speak, it speaks back
genesis discord               # chat from your phone
genesis mission "write a retry decorator with exponential backoff"
```

## What it can do

- **Run commands** through a three-level safety gate (see below)
- **Read and write files**, and finish multi-step work across turns instead of
  stopping after the first tool call
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

Ten verified, dependency-free skills ship in `skills/` — enough to see the
format and the reuse working. They are not the product; the mechanism is.
Point the agent at real work, or run the forge, and the library becomes yours:

```bash
python -m genesis_agent.parallel_forge --n 12
```

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

Issues and PRs welcome. If you are changing behaviour the agent depends on, run
the integration test first:

```bash
python scripts/e2e_integration_test.py
```

## License

MIT — see [LICENSE](LICENSE).
