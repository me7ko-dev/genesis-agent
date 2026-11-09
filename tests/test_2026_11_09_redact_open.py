"""notifier.redact — отвореното (ниско) от одита на 2026-10-31.

Мерено на 2026-11-09 с `redact()` върху 29 реда, тайна `hunter2XYZ`: от
изброените в NEXT_STEPS форми изтичаха всичките — `--passphrase x`,
`PASSPHRASE=x`, `rar -hpx`, `"APIKEY": "x"`, `d['password'] = 'x'`, — а покрай
тях и `ssh-keygen -N x` и сравнението `d['password'] == 'x'`. Всеки ред тук
падаше преди поправката; „не са тайни“ отдолу пази да не се маскира излишно.
"""
from __future__ import annotations

import time

import pytest

from genesis_agent.notifier import redact

S = "hunter2XYZ"

LEAKED = [
    # passphrase — флаг, променлива на средата, ключ в YAML/JSON
    f"gpg --batch --passphrase {S} -c f.txt",
    f"gpg --passphrase={S} -c f.txt",
    f"borg init --key-passphrase {S}",
    f"PASSPHRASE={S}",
    f"export BORG_PASSPHRASE={S}",
    f"KEYPASSPHRASE={S}",
    f"passphrase: {S}",
    f'{{"passphrase": "{S}"}}',
    # rar: -hp шифрова и имената (самото -p се маскираше)
    f"rar a -hp{S} a.rar docs",
    f"Rar.exe a -hp{S} a.rar",
    f"ssh-keygen -t ed25519 -N {S} -f id",
    # ключ в кавички, слепен с api/auth/access/db без граница
    f'{{"APIKEY": "{S}"}}',
    f'{{"apikey": "{S}"}}',
    f"{{'APIKEY': '{S}'}}",
    f'{{"x-apikey": "{S}"}}',
    f'{{"authtoken": "{S}"}}',
    # индекс вместо литерал на речник
    f"d['password'] = '{S}'",
    f'd["password"] = "{S}"',
    f"os.environ['API_KEY'] = '{S}'",
    f"cfg['db']['passwd']='{S}'",
    f"if d['password'] == '{S}':",
    f"assert cfg['token'] != '{S}'",
]

HARMLESS = [
    "passphrases: many",
    "gpg --no-passphrase x",
    "gpg --passphrase-fd 0 -c f.txt",
    "gpg --passphrase-file /run/p -c f.txt",
    "ls -hp /tmp",
    "du -hp x",
    "rar a -hp a.rar",            # без стойност: rar пита сам
    "ssh-keygen -N",
    '{"monkey": "banana"}',
    '{"turkey": "banana"}',
    '{"compass": "north"}',
    "d['monkey'] = 'banana'",
    "d['path'] == 'x'",
    "--- PASS: TestX (0.00s)",
]


@pytest.mark.parametrize("line", LEAKED, ids=range(len(LEAKED)))
def test_secret_is_masked(line: str) -> None:
    assert S not in redact(line), redact(line)


@pytest.mark.parametrize("line", HARMLESS, ids=range(len(HARMLESS)))
def test_harmless_line_is_untouched(line: str) -> None:
    assert redact(line) == line


def test_only_the_value_is_replaced() -> None:
    # Командата остава четима в известието — маскира се само стойността.
    assert redact(f"rar a -hp{S} a.rar docs") == "rar a -hp*** a.rar docs"
    assert redact(f"ssh-keygen -t ed25519 -N {S} -f id") == "ssh-keygen -t ed25519 -N *** -f id"
    assert redact(f"d['password'] = '{S}'") == "d['password'] = \"***\""


def test_long_hostile_line_stays_fast() -> None:
    # debug.log минава всеки запис през redact — новите правила не бива да
    # връщат квадратичното време (2026-10-31: 50 000 точки отнемаха 8 s).
    for unit in ("'password']", "rar -hp ", "passphrase", '"apikey" ', "ssh-keygen -N-"):
        line = unit * (50_000 // len(unit))
        t0 = time.perf_counter()
        redact(line)
        assert time.perf_counter() - t0 < 2.0, unit
