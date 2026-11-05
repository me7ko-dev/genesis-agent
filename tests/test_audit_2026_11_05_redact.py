"""Одит 2026-11-05: отвореното от петия кръг в notifier.redact.

`-phunter2` (слепено, mysql/mariadb/sshpass) минаваше; `BYPASS:`, `COMPASS:`,
`--- PASS: TestX` се маскираха като ключове.
"""
from __future__ import annotations

import pytest

from genesis_agent.notifier import redact


@pytest.mark.parametrize("text", [
    "mysql -uroot -phunter2 shop",
    "mysqldump -u root -phunter2 shop > dump.sql",
    "mariadb -h db -phunter2",
    "mariadb-dump -phunter2 shop",
    "sshpass -phunter2 ssh deploy@host",
    "sshpass -p'hunter2' scp a host:/tmp",
])
def test_password_glued_to_p_flag_is_masked_for_db_and_sshpass(text) -> None:
    out = redact(text)
    assert "hunter2" not in out, out
    assert "-p***" in out


@pytest.mark.parametrize("text", [
    "mkdir -pv build/out",
    "cp -pr src dst",
    "ssh -p2222 host",
    "git log -p",
])
def test_glued_p_elsewhere_is_left_alone(text) -> None:
    assert redact(text) == text


def test_glued_p_masking_stays_on_the_mysql_line() -> None:
    text = "mysql -uroot -phunter2 shop\nmkdir -pv build"
    assert redact(text) == "mysql -uroot -p*** shop\nmkdir -pv build"


@pytest.mark.parametrize("text", [
    "BYPASS: не",
    "COMPASS: север",
    "TURKEY: Istanbul",
    "MONKEY: patch",
    "--- PASS: TestParse (0.00s)",
])
def test_uppercase_words_ending_in_pass_or_key_are_not_secrets(text) -> None:
    assert redact(text) == text


@pytest.mark.parametrize("text", [
    "PGPASSWORD=hunter2 psql",
    "VAULTTOKEN=hunter2",
    "APIKEY: hunter2",
    "PASS: hunter2",
    "DB_PASS=hunter2",
    "MYSQL_PWD=hunter2 mysql",
    "AWS_SECRET_ACCESS_KEY=hunter2",
])
def test_env_style_secrets_are_still_masked(text) -> None:
    assert "hunter2" not in redact(text)
