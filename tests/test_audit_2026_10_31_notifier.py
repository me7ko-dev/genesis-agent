"""Одит 2026-10-31 (шести кръг): genesis_agent/notifier.py — токенът на самия
Telegram бот (интервал/\\r в променливата на средата → InvalidURL с токена в
debug.log, а известията мълчаливо спират), тайни под ключ в кавички (JSON,
dict repr), паролите във флагове (`-phunter2`, `--user u:p`, `--api-key x`),
стойност в кавички с интервали, `redis://:парола@`, прекаленото маскиране на
`BYPASS:`/`--- PASS:`, Markdown, пратен като HTML, и рязане на командата/целта
ПРЕДИ маскирането. Всеки тест падаше срещу кода преди поправката."""
from __future__ import annotations

import http.client
import json
import logging
import random
import re
import shlex
import time

import pytest

from genesis_agent import debug_log, notifier, paths
from genesis_agent.notifier import redact

# conftest.py подменя notifier.resolve_setting с празна функция за всеки тест
# (никакви истински известия). Истинската се взима тук, при импорта на модула —
# преди фикстурите — за тестовете, които проверяват точно нея; там urlopen е
# фалшив, така че мрежа няма.
_REAL_RESOLVE_SETTING = notifier.resolve_setting

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
SECRET = TOKEN.split(":", 1)[1]


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_urlopen(sent: list[tuple[str, dict]]):
    def urlopen(req, timeout=None):
        # Истинската проверка на адреса от http.client, без мрежа: putrequest
        # само валидира и буферира, връзка се отваря чак при send(). Точно тя
        # хвърля InvalidURL за токен с интервал или \r.
        http.client.HTTPConnection(req.host).putrequest(req.get_method(), req.selector)
        sent.append((req.full_url, json.loads(req.data.decode("utf-8"))))
        return _Resp()
    return urlopen


@pytest.fixture
def telegram(monkeypatch) -> list[tuple[str, dict]]:
    """Конфигуриран Telegram с фалшив urlopen: връща записаните заявки."""
    sent: list[tuple[str, dict]] = []
    settings = {"GENESIS_TELEGRAM_TOKEN": TOKEN, "GENESIS_TELEGRAM_CHAT_ID": "42"}
    monkeypatch.setattr(notifier, "resolve_setting", lambda key, **kw: settings[key])
    monkeypatch.setattr(notifier.urllib.request, "urlopen", _fake_urlopen(sent))
    return sent


def _debug_log_text() -> str:
    for name in debug_log.LOGGER_NAMES:
        for h in logging.getLogger(name).handlers:
            h.flush()
    p = paths.GENESIS_HOME / debug_log.FILE_NAME
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ── 1. Токенът на бота: интервал/\r → InvalidURL с токена в debug.log ──


@pytest.mark.parametrize("junk", [" ", "\r"])
def test_whitespace_around_the_telegram_settings_is_stripped(monkeypatch, junk) -> None:
    """Windows `set GENESIS_TELEGRAM_TOKEN=123:abc ` пази интервала, а
    `export X=$(cat token.txt)` от CRLF файл — \\r."""
    monkeypatch.setenv("GENESIS_TELEGRAM_TOKEN", TOKEN + junk)
    monkeypatch.setenv("GENESIS_TELEGRAM_CHAT_ID", "42" + junk)
    assert _REAL_RESOLVE_SETTING("GENESIS_TELEGRAM_TOKEN") == TOKEN
    assert _REAL_RESOLVE_SETTING("GENESIS_TELEGRAM_CHAT_ID") == "42"


@pytest.mark.parametrize("junk", [" ", "\r"])
def test_a_token_with_stray_whitespace_is_delivered_and_never_lands_in_debug_log(
        monkeypatch, junk) -> None:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(notifier, "resolve_setting", _REAL_RESOLVE_SETTING)
    monkeypatch.setattr(notifier.urllib.request, "urlopen", _fake_urlopen(sent))
    monkeypatch.setenv("GENESIS_TELEGRAM_TOKEN", TOKEN + junk)
    monkeypatch.setenv("GENESIS_TELEGRAM_CHAT_ID", "42" + junk)
    debug_log.install()
    ok = notifier.notify("тест")
    assert SECRET not in _debug_log_text()
    assert ok is True
    assert sent[0][0] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert sent[0][1]["chat_id"] == "42"


def test_an_invalid_url_never_escapes_send_telegram_or_prints_the_token(monkeypatch,
                                                                        capsys) -> None:
    """InvalidURL е HTTPException, не URLError — минаваше покрай except-а."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(notifier.urllib.request, "urlopen", _fake_urlopen(sent))
    assert notifier._send_telegram("тест", TOKEN + " x", "42") is False
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert "Telegram" in out.out          # операторът вижда защо не тръгва
    assert sent == []


@pytest.mark.parametrize("text", [
    TOKEN,
    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    f"http.client.InvalidURL: URL can't contain control characters. '/bot{TOKEN} /sendMessage'",
    f"token {TOKEN}",
])
def test_a_telegram_bot_token_is_masked_bare_and_inside_the_api_url(text) -> None:
    out = redact(text)
    assert SECRET not in out, out
    assert "***" in out


def test_the_api_host_stays_readable_around_a_masked_bot_token() -> None:
    out = redact(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    assert out.startswith("https://api.telegram.org/bot") and out.endswith("/sendMessage")


# ── 2. Тайни под ключ в кавички (JSON, dict repr, AWS CLI) ──


@pytest.mark.parametrize("text", [
    '{"username": "admin", "password": "hunter2"}',
    "{'api_key': 'hunter2', 'model': 'x'}",
    '{"SecretAccessKey": "hunter2/wJalrXUtnFEMIK7MDENG", "AccessKeyId": "id-1"}',
    "headers={'X-Api-Key': 'hunter2hunter2'}",
    '"access_token":"hunter2hunter2"',
    '{"apiKey": "hunter2"}',
    "['password' => 'hunter2']",
    'curl -d "{\\"password\\": \\"hunter2\\"}" https://h/login',
    '{"client_secret": "hunter2 with spaces"}',
])
def test_secrets_under_quoted_keys_are_masked(text) -> None:
    out = redact(text)
    assert "hunter2" not in out, out
    assert "***" in out


def test_a_json_login_body_keeps_its_shape() -> None:
    assert (redact('{"username": "admin", "password": "hunter2"}')
            == '{"username": "admin", "password": "***"}')


@pytest.mark.parametrize("text", [
    '{"AccessKeyId": "id-123", "Region": "eu-west-1"}',
    '{"monkey": "banana", "compass": "north", "passage": 3}',
    "{'username': 'admin', 'keyboard': 'us'}",
])
def test_ordinary_quoted_keys_stay_readable(text) -> None:
    assert redact(text) == text


# ── 3. Пароли във флагове ──


@pytest.mark.parametrize("text", [
    "mysql -uroot -phunter2 db",
    "mysql -u root -p'hunter2' db",
    "mysqldump -uroot -phunter2 shop > dump.sql",
    "sshpass -phunter2 ssh host",
    "7z a -phunter2 out.7z dir",
    "sqlcmd -S srv -U sa -P hunter2",
    "zip -r -P hunter2 out.zip dir",
    "curl --user admin:hunter2 https://h",
    "curl -uadmin:hunter2 https://h",
    "tool --api-key hunter2abc",
    "tool --secret hunter2abc",
    "tool --client-secret hunter2abc",
    "wget --http-password hunter2 https://h",
    "redis-cli -a hunter2 ping",
])
def test_passwords_in_glued_quoted_and_long_flags_are_masked(text) -> None:
    out = redact(text)
    assert "hunter2" not in out, out
    assert "***" in out


@pytest.mark.parametrize("text", [
    "find . -name '*.py' -print",
    "gcc -pthread -pedantic main.c",
    "scp -P 2222 file.txt host:/tmp/",
    "pip install --user requests",
    "mysql -uroot shop",
    "psql --no-password -h localhost",
])
def test_flags_that_are_not_passwords_stay_readable(text) -> None:
    assert redact(text) == text


# ── 4. Стойност в кавички с интервали — маскираше се само първата дума ──


@pytest.mark.parametrize(("text", "expected"), [
    ('DB_PASSWORD="correct horse battery staple" ./migrate.sh', "DB_PASSWORD=*** ./migrate.sh"),
    ("API_KEY='abc def ghi'", "API_KEY=***"),
    ("export SECRET='my long pass phrase'", "export SECRET=***"),
    ('password = "two words"', "password=***"),
    ("mysqldump --password='hunter 2' db", "mysqldump --password *** db"),
])
def test_a_quoted_value_with_spaces_is_masked_whole(text, expected) -> None:
    assert redact(text) == expected


# ── 5. URL с празно потребителско име (redis://:парола@) ──


@pytest.mark.parametrize(("text", "expected"), [
    ("REDIS_URL=redis://:hunter2@cache.internal:6379/0",
     "REDIS_URL=redis://***@cache.internal:6379/0"),
    ("celery -A app worker -b redis://:hunter2@cache:6379/0",
     "celery -A app worker -b redis://***@cache:6379/0"),
    ("https://:hunter2@host/x", "https://***@host/x"),
])
def test_url_credentials_without_a_user_are_masked(text, expected) -> None:
    assert redact(text) == expected


# ── 6. BYPASS:/COMPASS:/`--- PASS:` не са тайни ──


@pytest.mark.parametrize("text", [
    "BYPASS: yes",
    "COMPASS: 270",
    "SURPASS: x",
    "--- PASS: TestAdd (0.00s)",
])
def test_uppercase_words_ending_in_pass_are_not_masked(text) -> None:
    # Голото `PASS: x` вече се маскира (сливане с одита 2026-11-05 от main):
    # `PASS: hunter2` и `PASS: test_login` не се различават, а изтекла тайна е
    # по-лошо от маскиран ред на тест.
    assert redact(text) == text


@pytest.mark.parametrize("text", [
    "PGPASSWORD=hunter2 psql",
    "MYSQL_PWD=hunter2 mysql",
    "GH_PAT=hunter2 gh auth",
    "PASS=hunter2 ./run.sh",
    "DBPASS=hunter2",
    "APIKEY=hunter2",
    "pass: hunter2",
])
def test_env_style_secrets_are_still_masked(text) -> None:
    out = redact(text)
    assert "hunter2" not in out, out


# ── 7. Markdown, пратен с parse_mode=HTML ──


def test_markdown_markers_become_telegram_html(telegram) -> None:
    notifier.send_message("🛡️ **Genesis Sandbox** блокира опасна команда:\n"
                          "`rm -rf / --no-preserve-root`\n🛡️ Блокирано")
    body = telegram[0][1]
    assert body["parse_mode"] == "HTML"
    assert body["text"] == ("🛡️ <b>Genesis Sandbox</b> блокира опасна команда:\n"
                            "<code>rm -rf / --no-preserve-root</code>\n🛡️ Блокирано")


def test_markup_in_the_content_stays_text_and_tags_never_nest(telegram) -> None:
    notifier.send_message("**Genesis** <b>x</b> `a **b** && c<d>`")
    assert telegram[0][1]["text"] == ("<b>Genesis</b> &lt;b&gt;x&lt;/b&gt; "
                                      "<code>a **b** &amp;&amp; c&lt;d&gt;</code>")


def test_a_redaction_marker_is_not_taken_for_bold(telegram) -> None:
    notifier.send_message("password=hunter2 **Genesis**")
    assert telegram[0][1]["text"] == "password=*** <b>Genesis</b>"


# ── 8. Рязане ПРЕДИ маскирането: срязан адрес губи `@` и паролата минава ──

_TAIL = " && cd /srv/app && git pull https://me:hunter2pass@git.example.com/app.git && rm -rf ~"
_UNTIL_PASSWORD = _TAIL[:_TAIL.index("@")]


def test_a_long_blocked_command_cut_before_the_at_sign_leaks_no_password(telegram,
                                                                        monkeypatch) -> None:
    import genesis_skills as gs
    from genesis_agent import sandbox

    command = "echo " + "x" * (300 - len("echo ") - len(_UNTIL_PASSWORD)) + _TAIL
    assert command[:300].endswith("hunter2pass")
    blocked = sandbox.SandboxResult(False, "", "[SANDBOX BLOCKED] рекурсивно триене", None,
                                    blocked=True)
    monkeypatch.setattr(gs.sandbox, "run_shell", lambda *a, **kw: blocked)
    monkeypatch.setattr(gs, "_log_episode", lambda *a, **kw: None)
    gs._tool_run_cmd(command)
    assert len(telegram) == 1
    text = telegram[0][1]["text"]
    assert "hunter2pass" not in text, text
    assert "SANDBOX BLOCKED" in text


@pytest.mark.parametrize("success", [True, False])
def test_a_long_mission_goal_cut_before_the_at_sign_leaks_no_password(telegram, monkeypatch,
                                                                      success) -> None:
    from genesis_agent import autonomous_loop as al
    from genesis_agent import reflection

    goal = "Направи " + "x" * (200 - len("Направи ") - len(_UNTIL_PASSWORD)) + _TAIL
    assert goal[:200].endswith("hunter2pass")
    outcome = al.LoopOutcome(success=success, rounds=2, skill_path="/x/skills/app.py",
                             last_stdout="", last_stderr="", storage_note="", dna_audit={})
    monkeypatch.setattr(al, "_run_autonomous_loop_impl", lambda *a, **kw: outcome)
    monkeypatch.setattr(reflection, "record_mission", lambda *a, **kw: None)
    al.run_autonomous_loop(goal, max_rounds=1)
    assert len(telegram) == 1
    text = telegram[0][1]["text"]
    assert "hunter2pass" not in text, text
    assert "Направи" in text


# ── 9. След прегледа (2026-11-05): стойността е цялата shell дума ──
# `"…"|'…'|\S+` спираше на първата затваряща кавичка и опашката оставаше
# видима. На 5c7462a падат 16 от 17; на a4fd75c (`\S+` — цялата дума) — 6:
# флаговете, които там не се познаваха (слепено `-p`, `redis-cli -a`,
# `sqlcmd -P`), и стойностите с интервал в кавички. Другите пазят изтичането
# да не се върне.

_QUOTED_PW = shlex.quote("Xy7'q9!secret")    # 'Xy7'"'"'q9!secret'


@pytest.mark.parametrize("text", [
    f"export DB_PASSWORD={_QUOTED_PW} && ./migrate",
    f"mysql --password {_QUOTED_PW} db",
    f"mysql --password={_QUOTED_PW} db",
    f"mysql -uroot -p{_QUOTED_PW} db",
    f"sshpass -p {_QUOTED_PW} ssh host",
    f"sshpass -p{_QUOTED_PW} ssh host",
    f"redis-cli -a {_QUOTED_PW} ping",
    f"sqlcmd -S srv -U sa -P {_QUOTED_PW}",
])
def test_a_shlex_quoted_password_with_an_apostrophe_is_masked_whole(text) -> None:
    out = redact(text)
    assert "q9!secret" not in out, out
    assert "Xy7" not in out, out


@pytest.mark.parametrize(("text", "expected"), [
    ('PASSWORD="abc"def ./run.sh', "PASSWORD=*** ./run.sh"),
    ("API_KEY='part1'\"part2\" make", "API_KEY=*** make"),
    ("mysql --password='abc'\\''def' db", "mysql --password *** db"),
    ('TOKEN=abc"def ghi" ./run.sh', "TOKEN=*** ./run.sh"),
    ("curl -u 'me:a b'\"c\" https://h", "curl -u *** https://h"),
])
def test_quoted_parts_glued_into_one_word_are_masked_together(text, expected) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize("text", [
    r"chrome --user-data-dir=C:\Users\x",
    "curl --user-agent=a:b https://h",
    "gcc $(mysql_config --cflags) -pthread main.c",
    "cc main.c $(mariadb_config --libs) -pthread",
])
def test_flags_that_only_start_like_a_password_flag_stay_readable(text) -> None:
    """`--user-data-dir`, `--user-agent`, `mysql_config` не носят парола."""
    assert redact(text) == text


# ── 10. `user:pass@` без `://` назад-напред по дълъг ред (2026-11-05) ──
# Правилото опитваше от всяка позиция след `.`/`-`/`+` и сканираше реда до
# края: 50 000 точки — 8 s, 50 000 тирета — 7 s (debug.log минава всеки запис
# през redact, чатът стоеше). Сега един проход.

_OLD_USERPASS = re.compile(r"(?<![\w/@:])[\w.+-]+:[^\s@/:]+@(?=[\w.-]+)")


@pytest.mark.parametrize("line", [
    "." * 60000 + " a:b@c",
    "-" * 60000 + " a:b@c",
    "a." * 30000 + ":x y@z",
    "/" + "ab-" * 20000 + ":pw@host",
    # ids: без тях името на случая е самият ред от 60 000 знака, а Windows
    # отказва PYTEST_CURRENT_TEST над 32 767 („environment variable is longer
    # than 32767 characters“ — 8 грешки на лаптопа, 2026-11-09; същото като #80).
], ids=["dots", "dashes", "a-dot", "ab-dash"])
def test_long_runs_of_dots_or_dashes_are_redacted_in_linear_time(line) -> None:
    t0 = time.perf_counter()
    redact(line)
    assert time.perf_counter() - t0 < 1.5


def test_user_pass_at_rule_masks_exactly_what_the_old_regex_masked() -> None:
    """Същите съвпадения като стария израз — само без квадратичното време."""
    rnd = random.Random(20261105)
    alphabet = "ab.-+/@: é"
    for _ in range(4000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 14)))
        assert notifier._mask_user_pass_at(s) == _OLD_USERPASS.sub("***@", s), repr(s)
    for s in ["me:pw@host", "x.user:pass@h.com", "/a-user:pw@host", "@a.b:c@d",
              "git@github.com:org/r", "a:b@", "a:b@-x", "ftp://u:p@h"]:
        assert notifier._mask_user_pass_at(s) == _OLD_USERPASS.sub("***@", s), s


# ── 11. Сливането с #108 (2026-11-05): регистърът ──


@pytest.mark.parametrize("text", [
    "PGPassword=hunter2 psql",
    "SMTPPassword: hunter2",
    "JWTSecret=hunter2",
    "VAULTtoken=hunter2",
    r"C:\MySQL\bin\MySQL.exe -uroot -phunter2 shop",
    "MYSQL -uroot -phunter2",
    "Sshpass -phunter2 ssh h",
])
def test_mixed_case_keys_and_program_names_are_still_masked(text) -> None:
    assert "hunter2" not in redact(text), redact(text)


@pytest.mark.parametrize("text", ["TURKEY: Istanbul", "HOTKEY: F5", "mysql -P 3306 -h db"])
def test_mixed_case_rules_leave_ordinary_text_alone(text) -> None:
    assert redact(text) == text
