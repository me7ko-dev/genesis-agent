"""redact и паметта — отвореното от 10.11, мерено 2026-11-15 с тайна `hunter2XYZ`.

notifier.redact (известия, debug.log, episodes.db):
- изтичаха `password == "…"`, `ssh-keygen -p -P стара`, `WinRAR -hp…`,
  `{"pass_phrase": …}`;
- `if event['key'] == 'Enter':` ставаше `== "***"` — `'key'` в сравнение е име
  на поле, не ключ.
workspace_memory.mask_secrets (нишки, бележки, conversation_memory):
- `api_key=os.environ["OPENAI_API_KEY"]` се режеше — препратка, не тайна;
- `{"password": "…"}` и `mysql -p…` оставаха.
"""
from __future__ import annotations

import pytest

from genesis_agent.notifier import redact
from genesis_agent.workspace_memory import mask_secrets

S = "hunter2XYZ"


@pytest.mark.parametrize("line", [
    f'if password == "{S}":',
    f"if user.password != '{S}':",
    f"ssh-keygen -p -P {S} -N new{S} -f id_ed25519",
    f"WinRAR a -hp{S} a.rar docs",
    f"winrar.exe a -hp{S} a.rar docs",
    f'{{"pass_phrase": "{S}"}}',
    f"pass_phrase: {S}",
])
def test_these_no_longer_leak(line):
    assert S not in redact(line), redact(line)


@pytest.mark.parametrize("line", [
    f"ssh-keygen -p -P {S} -N x -f k", f"WinRAR a -hp{S} a.rar d", f'{{"pass_phrase": "{S}"}}',
])
def test_tool_and_key_forms_also_in_memory(line):
    assert S not in redact(line, memory=True)


@pytest.mark.parametrize("line", [
    "if event['key'] == 'Enter':",
    'if e["key"] === "Escape") {',
    "while d['key'] != 'q':",
])
def test_a_key_field_compared_to_a_word_is_not_a_secret(line):
    assert redact(line) == line
    assert redact(line, memory=True) == line


def test_a_key_field_compared_to_a_real_secret_still_masks():
    assert S not in redact(f"if d['password'] == '{S}':")
    assert S not in redact(f"if d['api_key'] == '{S}':")


@pytest.mark.parametrize("line", [
    f'{{"password": "{S}"}}',
    '{"password": "correcthorsebattery"}',
    f"mysql -u root -p{S} shop",
    f"mysqldump -uroot -p{S} shop > d.sql",
    f"MySQL.exe -p{S}",
])
def test_memory_masks_json_passwords_and_mysql(line):
    out = mask_secrets(line)
    assert S not in out and "correcthorsebattery" not in out, out


@pytest.mark.parametrize("line", [
    'api_key=os.environ["OPENAI_API_KEY"]',
    "token = os.getenv('GITHUB_TOKEN_FOR_DEPLOY')",
    "secret = process.env.STRIPE_SECRET_KEY_LIVE",
    "sort_key=created_at",
    "API_KEY=$API_KEY",
    '{"password": ""}',
    "mkdir -p build",
])
def test_memory_keeps_references_and_plain_words(line):
    assert mask_secrets(line) == line
