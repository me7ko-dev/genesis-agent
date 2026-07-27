# Security

Genesis Agent runs shell commands, writes files, and drives a browser on your
machine. That is the point of it, and it is also the risk. This document is
about what the safety layer actually does — and what it does not.

## Reporting a vulnerability

Open a [security advisory](../../security/advisories/new) rather than a public
issue. Please include a reproduction. There is no bounty; this is a personal
open-source project.

## The threat model, stated plainly

**The sandbox protects you from an LLM making a mistake. It does not protect
you from someone deliberately attacking you.**

`genesis_agent/sandbox.py` classifies every command, file operation, and
browser action into three levels:

| Level | Behaviour |
|---|---|
| `SAFE` | runs automatically |
| `CONFIRM` | asks you first (or is refused outright in unattended mode) |
| `BLOCKED` | never runs, in any mode, however it is asked |

The classifier is pattern- and structure-based. A determined adversary who can
choose the exact command text can very likely find a phrasing it does not
recognise. It is a guardrail, not a jail.

**If you plan to run Genesis unattended, on untrusted goals, or on a machine
with anything valuable on it, put it in a container.** Docker, Podman, or a VM.
The sandbox is a good second layer and a poor first one.

## What is blocked unconditionally

These are refused even in `allow` mode, even if you explicitly ask for them:

- `rm -rf /` and equivalents, fork bombs, `mkfs`, raw `dd` to a device
- Typing into a password field, a card/CVV/IBAN/SSN-shaped field, or a
  private-key field in the browser
- Clicking "buy now" / "place order" / "confirm payment"

The browser rules exist because an agent that can spend your money or leak your
credentials is a different category of problem from an agent that can delete a
file. They are not configurable, on purpose.

## Your API keys

- Keys live in `~/.genesis/.env`, mode `600`. They are never committed —
  `.env` is in `.gitignore`, and `config.yaml` (which *is* committed) holds no
  secrets.
- Commands the agent runs get a **minimal environment**. Your keys are not in
  it, so a generated script cannot read them and phone home.
- Findings reported to Discord pass through `redact_secrets()` first, so a
  model that quotes a line from a `.env` file does not publish your key to a
  chat channel.

If you ever paste a key into a chat, a commit, or a log — rotate it. Providers
issue new keys for free; assuming an exposed key is still private is how
accounts get drained.

## One key per provider — a deliberate limitation

Genesis supports **one API key per provider**. It will not rotate multiple
accounts of the same provider to multiply a free-tier quota.

This is not an oversight. Most providers' terms of service prohibit creating
multiple accounts to circumvent rate limits. The realistic consequence is
having your accounts terminated, and a tool that ships quota-circumvention as
a feature is a liability for everyone who installs it. Headroom here comes
from breadth — six providers, each used within its own limits — which is both
more robust and entirely above board.

## The browser

`genesis_agent/browser.py` launches Chromium with an **ephemeral, isolated
profile**. It cannot see your real cookies, saved passwords, or logged-in
sessions. Every run starts blank. That means the agent cannot act as you on a
site you are logged into — which is a feature, not a missing one.

## Autonomous mode

The 24/7 loop (`!start24_7` in Discord) sets the sandbox policy to `deny`:
anything at `CONFIRM` level is refused rather than queued, because there is
nobody there to answer. The preparatory worker (`thread_worker.py`) is
narrower still — it may only read files, list directories, and search the web.
It cannot run commands, write files, or drive the browser. It gathers
information while you sleep; it does not change your machine while you sleep.

## Known, honest limitations

- **Models sometimes fabricate instead of calling a tool.** Native
  tool-calling makes this much rarer and usually self-correcting, but it is not
  eliminated. Check summaries against the actual tool output when it matters.
- **The skill library is generated code.** Skills marked `verified` really did
  run their self-test in the sandbox; that is a much lower bar than "audited".
  Read a skill before trusting it with anything important.
- **Prompt injection is not solved.** A web page or file the agent reads can
  contain text aimed at the agent. The sandbox limits the blast radius; it does
  not detect the attempt.
