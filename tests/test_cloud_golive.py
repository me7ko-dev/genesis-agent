"""Готово ли е за live (cloud/golive_check.py) и правните страници (2026-11-13).

Всяка проверка: пълна, наред настройка минава, а развалено само едно нещо
пада точно на своята проверка — без нея тестът би минал."""
from __future__ import annotations

import http.client
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from cloud import golive_check as gl
from cloud.web import legal
from cloud.web import server as web
from cloud.web.store import Store

ROOT = Path(__file__).resolve().parent.parent
LIVE = "sk_live_" + "a1B2" * 6
HOOK = "whsec_" + "Zz9" * 11
VALUES = {
    "ПРОДАВАЧ": "Иван Петров", "ИДЕНТИФИКАТОР": "няма", "АДРЕС": "ул. Първа 1, София",
    "ИМЕЙЛ": "help@genesis.example.eu", "ХОСТИНГ": "Хостинг ООД, Германия",
    "ДОСТАВЧИЦИ_НА_МОДЕЛИ": "Groq, Inc., САЩ", "СРОК_РАЗГОВОРИ": "90 дни след последния ход",
    "В_СИЛА_ОТ": "2026-12-01", "ПРЕГЛЕДАНО": "да",
}


def _prices(**over: Any) -> dict[str, Any]:
    data = {
        "default": {"input": 300, "cached": 75, "output": 1200},
        "models": {"groq/openai/gpt-oss-120b": {"input": 40, "cached": 20, "output": 150}},
        "per_turn": 2, "min_turn_tokens": 20000,
        "packs": {"p5": {"credits": 500, "amount_cents": 500, "currency": "eur",
                         "stripe_price": "price_p5"}},
        "plans": {"basic": {"credits": 1500, "amount_cents": 1500, "currency": "eur",
                            "stripe_price": "price_basic"}},
    }
    data.update(over)
    return data


def _stripe_list(**over: dict[str, Any]) -> dict[str, Any]:
    p5 = {"id": "price_p5", "livemode": True, "active": True, "unit_amount": 500,
          "currency": "eur", "type": "one_time", "recurring": None, **over.get("p5", {})}
    basic = {"id": "price_basic", "livemode": True, "active": True, "unit_amount": 1500,
             "currency": "eur", "type": "recurring",
             "recurring": {"interval": "month", "interval_count": 1}, **over.get("basic", {})}
    return {"object": "list", "data": [p5, basic]}


class Setup:
    """Една пълна live настройка в tmp — после всеки тест разваля едно нещо."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.prices = tmp / "prices.json"
        self.keys = tmp / "keys.env"
        self.legal = tmp / "legal.json"
        self.stripe_prices = tmp / "stripe-prices.json"
        self.write(prices=_prices(), stripe=_stripe_list(), values=VALUES)
        self.keys.write_text("GATEWAY_SECRET=" + "s" * 40 + "\nGROQ_API_KEY=gsk_x\n"
                             "GATEWAY_MODELS=groq/openai/gpt-oss-120b\n", encoding="utf-8")
        self.keys.chmod(0o640)          # като на сървъра (up.sh); 644 е отделна проверка
        self.env ={"STRIPE_SECRET_KEY": LIVE, "STRIPE_WEBHOOK_SECRET": HOOK}
        self.args = {"--env-file": self.keys.as_posix(),
                     "--gateway-usage": (tmp / "usage.jsonl").as_posix(),
                     "--billing": None, "--public-url": "https://genesis.mydomain.eu",
                     "--prices": self.prices.as_posix(), "--legal": self.legal.as_posix()}

    def write(self, *, prices: Any = None, stripe: Any = None, values: Any = None) -> None:
        if prices is not None:
            self.prices.write_text(json.dumps(prices), encoding="utf-8")
        if stripe is not None:
            self.stripe_prices.write_text(json.dumps(stripe), encoding="utf-8")
        if values is not None:
            self.legal.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")

    def line(self) -> str:
        return " ".join(k if v is None else f"{k} {v}" for k, v in self.args.items())

    def run(self, *, stripe_prices: bool = True) -> list[gl.Check]:
        return gl.run_checks(self.line(), self.env,
                             stripe_prices=self.stripe_prices if stripe_prices else None,
                             repo=ROOT)


def _failed(checks: list[gl.Check]) -> list[str]:
    return [c.text for c in checks if not c.ok and not c.warn]


@pytest.fixture
def setup(tmp_path) -> Setup:
    return Setup(tmp_path)


def test_a_complete_setup_is_ready(setup) -> None:
    checks = setup.run()
    assert _failed(checks) == [], checks
    text = "\n".join(c.text for c in checks)
    assert LIVE not in text and HOOK not in text          # ключовете не се печатат


# (какво се разваля, част от текста на проверката, която трябва да падне)
BREAKS: list[tuple[str, str]] = [
    ("no_billing", "без --billing"),
    ("no_gateway", "без --gateway-usage"),
    ("test_key", "още тестов"),
    ("no_key", "STRIPE_SECRET_KEY липсва"),
    ("pk_key", "STRIPE_SECRET_KEY липсва"),
    ("no_hook", "STRIPE_WEBHOOK_SECRET липсва"),
    ("http_url", "не е https"),
    ("example_url", "пробен адрес"),
    ("prices_in_repo", "в репото"),
    ("no_price_id", "няма stripe_price"),
    ("no_offers", "няма какво да се купи"),
    ("unchecked", "не са сверени със Stripe"),
    ("amount_differs", "в Stripe е 499 eur"),
    ("currency_differs", "в Stripe е 500 usd"),
    ("test_price", "не е live цена"),
    ("inactive_price", "не е активна"),
    ("price_missing", "го няма в --stripe-prices"),
    ("plan_yearly", "не е месечна"),
    ("plan_one_time", "трябва recurring"),
    ("no_models", "GATEWAY_MODELS"),
    ("unpriced_model", "няма своя цена"),
    ("openai_wildcard", "изброй моделите поотделно"),
    ("nvidia", "само за тестове"),
    ("no_provider_key", "няма GROQ_API_KEY"),
    ("short_secret", "GATEWAY_SECRET"),
    ("no_cap", "месечният таван"),
    ("no_daily", "дневният лимит"),
    ("no_turn_cap", "--max-tokens-per-turn е 0"),
    ("no_legal", "няма --legal"),
    ("legal_empty", "не са попълнени ИМЕЙЛ"),
    ("legal_draft", "още „чернова“"),
    ("legal_bad_json", "невалиден JSON"),
]


def _break(s: Setup, what: str) -> None:
    keys = s.keys.read_text(encoding="utf-8")
    if what == "no_billing":
        del s.args["--billing"]
    elif what == "no_gateway":
        del s.args["--gateway-usage"]
    elif what == "test_key":
        s.env["STRIPE_SECRET_KEY"] = "sk_test_" + "a" * 24
    elif what == "no_key":
        del s.env["STRIPE_SECRET_KEY"]
    elif what == "pk_key":
        s.env["STRIPE_SECRET_KEY"] = "pk_live_" + "a" * 24
    elif what == "no_hook":
        s.env["STRIPE_WEBHOOK_SECRET"] = ""
    elif what == "http_url":
        s.args["--public-url"] = "http://genesis.mydomain.eu"
    elif what == "example_url":
        s.args["--public-url"] = "https://genesis.example.com"
    elif what == "prices_in_repo":
        s.args["--prices"] = (ROOT / "cloud" / "web" / "prices.json").as_posix()
    elif what == "no_price_id":
        p = _prices()
        p["packs"]["p5"]["stripe_price"] = ""
        s.write(prices=p)
    elif what == "no_offers":
        s.write(prices=_prices(packs={}, plans={}))
    elif what == "unchecked":
        s.stripe_prices = None  # type: ignore[assignment]
    elif what == "amount_differs":
        s.write(stripe=_stripe_list(p5={"unit_amount": 499}))
    elif what == "currency_differs":
        s.write(stripe=_stripe_list(p5={"currency": "usd"}))
    elif what == "test_price":
        s.write(stripe=_stripe_list(p5={"livemode": False}))
    elif what == "inactive_price":
        s.write(stripe=_stripe_list(basic={"active": False}))
    elif what == "price_missing":
        s.write(stripe={"data": [_stripe_list()["data"][1]]})
    elif what == "plan_yearly":
        s.write(stripe=_stripe_list(basic={"recurring": {"interval": "year"}}))
    elif what == "plan_one_time":
        s.write(stripe=_stripe_list(basic={"type": "one_time", "recurring": None}))
    elif what == "no_models":
        s.keys.write_text(keys.replace("GATEWAY_MODELS=groq/openai/gpt-oss-120b\n", ""),
                          encoding="utf-8")
    elif what == "unpriced_model":
        s.keys.write_text(keys.replace("gpt-oss-120b\n", "gpt-oss-120b,groq/other\n"),
                          encoding="utf-8")
    elif what == "openai_wildcard":
        p = _prices()
        p["models"]["openai/*"] = {"input": 300, "cached": 75, "output": 1200}
        s.write(prices=p)
        s.keys.write_text(keys.replace("gpt-oss-120b\n", "gpt-oss-120b,openai/*\n")
                          + "OPENAI_API_KEY=x\n", encoding="utf-8")
    elif what == "nvidia":
        p = _prices()
        p["models"]["nvidia/*"] = {"input": 100, "cached": 50, "output": 300}
        s.write(prices=p)
        s.keys.write_text(keys.replace("gpt-oss-120b\n", "gpt-oss-120b,nvidia/*\n")
                          + "NVIDIA_API_KEY=x\n", encoding="utf-8")
    elif what == "no_provider_key":
        s.keys.write_text(keys.replace("GROQ_API_KEY=gsk_x\n", ""), encoding="utf-8")
    elif what == "short_secret":
        s.keys.write_text(keys.replace("s" * 40, "s" * 20), encoding="utf-8")
    elif what == "no_cap":
        s.args["--monthly-cap"] = "0"
    elif what == "no_daily":
        s.args["--turns-per-day"] = "0"
    elif what == "no_turn_cap":
        s.args["--max-tokens-per-turn"] = "0"
    elif what == "no_legal":
        del s.args["--legal"]
    elif what == "legal_empty":
        s.write(values={**VALUES, "ИМЕЙЛ": " "})
    elif what == "legal_draft":
        s.write(values={k: v for k, v in VALUES.items() if k != "ПРЕГЛЕДАНО"})
    elif what == "legal_bad_json":
        s.legal.write_text("{", encoding="utf-8")
    else:  # pragma: no cover
        raise AssertionError(what)


@pytest.mark.parametrize(("what", "expect"), BREAKS, ids=[b[0] for b in BREAKS])
def test_each_missing_piece_is_named(setup, what, expect) -> None:
    _break(setup, what)
    failed = _failed(setup.run(stripe_prices=setup.stripe_prices is not None))
    assert any(expect in t for t in failed), failed


def test_cerebras_is_a_warning_not_a_blocker(setup) -> None:
    p = _prices()
    p["models"]["cerebras/gpt-oss-120b"] = {"input": 40, "cached": 20, "output": 100}
    setup.write(prices=p)
    keys = setup.keys.read_text(encoding="utf-8")
    setup.keys.write_text(keys.replace("gpt-oss-120b\n", "gpt-oss-120b,cerebras/gpt-oss-120b\n")
                          + "CEREBRAS_API_KEY=x\n", encoding="utf-8")
    checks = setup.run()
    assert _failed(checks) == []
    assert any(c.warn and "Enterprise" in c.text for c in checks)


def test_unreadable_server_args_are_named(setup) -> None:
    assert _failed(gl.run_checks("--port не-число", setup.env, repo=ROOT)) == \
        ["GENESIS_WEB_ARGS не се чете като аргументи на serve"]


def test_main_reads_the_files_the_service_reads(setup, capsys) -> None:
    defaults = setup.tmp / "genesis-web"
    defaults.write_text(f"# cloud/deploy.sh\nGENESIS_WEB_ARGS={setup.line()}\n", encoding="utf-8")
    stripe_env = setup.tmp / "stripe.env"
    stripe_env.write_text(f"STRIPE_SECRET_KEY={LIVE}\nSTRIPE_WEBHOOK_SECRET='{HOOK}'\n",
                          encoding="utf-8")
    stripe_env.chmod(0o600)
    argv = ["--defaults", str(defaults), "--stripe-env", str(stripe_env),
            "--stripe-prices", str(setup.stripe_prices)]
    assert gl.main(argv) == 0
    out = capsys.readouterr().out
    assert "ГОТОВО ЗА LIVE" in out and LIVE not in out and HOOK not in out
    stripe_env.write_text("STRIPE_SECRET_KEY=sk_test_" + "a" * 24 + f"\nSTRIPE_WEBHOOK_SECRET={HOOK}\n",
                          encoding="utf-8")
    assert gl.main(argv) == 1
    out = capsys.readouterr().out
    assert "НЕ Е ГОТОВО ЗА LIVE — липсват 1 неща" in out and "още тестов" in out
    assert gl.main(["--defaults", str(setup.tmp / "nope"), "--stripe-env",
                    str(setup.tmp / "nope2")]) == 1
    out = capsys.readouterr().out
    assert "пусни cloud/deploy.sh" in out and "STRIPE_SECRET_KEY и" in out


@pytest.mark.skipif(os.name == "nt", reason="права на файлове (Linux)")
def test_secret_files_others_can_read_are_named(setup, capsys) -> None:
    defaults = setup.tmp / "genesis-web"
    defaults.write_text(f"GENESIS_WEB_ARGS={setup.line()}\n", encoding="utf-8")
    stripe_env = setup.tmp / "stripe.env"
    stripe_env.write_text(f"STRIPE_SECRET_KEY={LIVE}\nSTRIPE_WEBHOOK_SECRET={HOOK}\n",
                          encoding="utf-8")
    stripe_env.chmod(0o644)
    setup.keys.chmod(0o644)
    assert gl.main(["--defaults", str(defaults), "--stripe-env", str(stripe_env),
                    "--stripe-prices", str(setup.stripe_prices)]) == 1
    out = capsys.readouterr().out
    assert "stripe.env се чете от всички" in out and "keys.env се чете от всички" in out


# ── правните страници ────────────────────────────────────────────────────────

@pytest.mark.parametrize("page", legal.PAGES)
@pytest.mark.parametrize("lang", legal.LANGS)
def test_every_page_is_a_draft_with_known_slots_and_links(page, lang) -> None:
    text = legal.template(page, lang)
    assert legal.slots(text) <= set(legal.FIELDS), legal.slots(text) - set(legal.FIELDS)
    assert "{{ИМЕЙЛ}}" in text or page == "terms" and "{{ИМЕЙЛ}}" in text
    assert "<!--draft-->" in text and ("не е правен съвет" in text or "not legal advice" in text)
    suffix = "?lang=en" if lang == "en" else ""
    for other in legal.PAGES:
        assert f'href="/legal/{other}{suffix}"' in text
    assert f'<html lang="{lang}">' in text
    # само /app.css: CSP е default-src 'self', вграден стил не се зарежда
    assert "<style" not in text and "<script" not in text


def test_values_fill_the_slots_escaped_and_review_drops_the_draft() -> None:
    values = {**VALUES, "ПРОДАВАЧ": "<script>alert(1)</script>"}
    values.pop("ПРЕГЛЕДАНО")
    html = legal.render("contact", "bg", values)
    assert "&lt;script&gt;" in html and "<script>alert" not in html
    assert legal.slots(html) == set() and "не е правен съвет" in html
    reviewed = legal.render("contact", "bg", {**values, "ПРЕГЛЕДАНО": "да"})
    assert "не е правен съвет" not in reviewed
    # непопълнено остава видимо, не празно
    assert "{{АДРЕС}}" in legal.render("contact", "bg", {"ПРОДАВАЧ": "x"})


def test_unknown_fields_in_the_values_file_are_refused(tmp_path) -> None:
    f = tmp_path / "legal.json"
    f.write_text(json.dumps({"ПРОДАВАЧ": "x", "ИМЕИЛ": "typo"}, ensure_ascii=False),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="ИМЕИЛ"):
        legal.load_values(f)


def _get(port: int, path: str) -> tuple[int, bytes]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def test_the_pages_are_served_without_login(tmp_path) -> None:
    app = web.App(Store(tmp_path / "web.db"), tmp_path / "jobs", secure_cookie=False, recover=False,
                  legal_values={"ПРОДАВАЧ": "Иван Петров"})
    srv = web.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        code, body = _get(port, "/legal/terms")
        assert code == 200 and "Условия за ползване" in body.decode() and "Иван Петров" in body.decode()
        code, body = _get(port, "/legal/privacy?lang=en")
        assert code == 200 and '<html lang="en">' in body.decode()
        for bad in ("/legal/nope", "/legal/../server.py", "/legal/terms.bg.html", "/legal/"):
            assert _get(port, bad)[0] in (401, 404), bad
    finally:
        srv.shutdown()
        srv.server_close()
        app.pool.shutdown(wait=True)


def test_serve_refuses_a_broken_legal_file(tmp_path, capsys) -> None:
    f = tmp_path / "legal.json"
    f.write_text("[]", encoding="utf-8")
    assert web.main(["serve", "--data", str(tmp_path), "--jobs", str(tmp_path / "j"),
                     "--legal", str(f), "--port", "0"]) == 2
    assert "текстови стойности" in capsys.readouterr().err


def test_the_app_links_the_pages_at_signup_payment_and_account() -> None:
    page = (ROOT / "cloud" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    signup = page.split('id="signup-form"', 1)[1].split("</form>", 1)[0]
    assert 'href="/legal/terms"' in signup and 'href="/legal/privacy"' in signup
    offers = page.split('id="offers-legal"', 1)[1].split("</p>", 1)[0]
    assert 'href="/legal/terms"' in offers and 'href="/legal/refunds"' in offers
    account = page.split('id="account"', 1)[1]
    assert all(f'href="/legal/{p}"' in account for p in legal.PAGES)
    js = (ROOT / "cloud" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert '$("offers-legal").hidden = offers.children.length === 0' in js


def test_caddy_keeps_ip_logs_no_longer_than_the_privacy_page_says() -> None:
    caddy = (ROOT / "cloud" / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "roll_keep_for 720h" in caddy
    assert "до 30 дни" in legal.template("privacy", "bg")


# ── изтриване по искане (ОРЗД) ───────────────────────────────────────────────
# Поверителността обещава „изтриваме профила, разговорите и файловете до 30
# дни“ — без команда операторът нямаше как (сметката е само с добавяне).

def test_forget_deletes_the_person_but_keeps_the_ledger(tmp_path, capsys) -> None:
    store = Store(tmp_path / "web.db")
    uid = store.add_user("ana@example.com", "correct-horse-battery")
    other = store.add_user("bob@example.com", "correct-horse-battery")
    token = store.new_session(uid)
    chat = store.new_chat(uid, "таен проект")
    turn = store.add_turn(chat, "моята тайна задача")
    store.add_event(turn, {"type": "answer", "text": "тайният отговор"})
    store.finish_turn(turn, ok=True, error="", tokens={}, seconds=1.0)
    store.set_job(turn, "genesis-task-1", 100)
    store.job_started(turn, 0.0)
    store.add_entry(uid, "stripe:cs_1", "purchase", 500 * 1_000_000)
    bob_chat = store.new_chat(other, "на Боб")
    files = tmp_path / "jobs" / str(uid) / chat
    files.mkdir(parents=True)
    (files / "report.csv").write_text("x", encoding="utf-8")
    (tmp_path / "jobs" / str(other)).mkdir()
    args = ["forget", "ana@example.com", "--data", str(tmp_path), "--jobs", str(tmp_path / "jobs")]

    assert web.main(args) == 1                       # сметката на хода не е окончателна
    assert "17 мин" in capsys.readouterr().err and files.exists()
    store.mark_settled(turn)
    assert web.main(args) == 0
    assert "deleted-" in capsys.readouterr().out

    assert not (tmp_path / "jobs" / str(uid)).exists()
    assert (tmp_path / "jobs" / str(other)).exists()
    assert store.user_by_email("ana@example.com") is None
    assert store.session_user(token) is None
    assert store.check_password(f"deleted-{uid}@invalid", "correct-horse-battery") is None
    assert store.account(uid)["disabled"] is True
    assert store.balance(uid) == 500 * 1_000_000      # сметката остава
    # и не остава в свободните страници на файла (secure_delete)
    raw = (tmp_path / "web.db").read_bytes()
    assert "тайна задача".encode() not in raw and "тайният отговор".encode() not in raw
    assert b"ana@example.com" not in raw
    with sqlite3.connect(tmp_path / "web.db") as c:
        assert c.execute("SELECT COUNT(*) FROM chats WHERE user_id = ?", (uid,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM turns WHERE chat_id = ?", (chat,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM events WHERE turn_id = ?", (turn,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM chats WHERE id = ?", (bob_chat,)).fetchone()[0] == 1


def test_forget_waits_for_a_running_turn(tmp_path, capsys) -> None:
    store = Store(tmp_path / "web.db")
    uid = store.add_user("ana@example.com", "correct-horse-battery")
    store.add_turn(store.new_chat(uid, "x"), "върви")
    assert web.main(["forget", "ana@example.com", "--data", str(tmp_path),
                     "--jobs", str(tmp_path / "jobs")]) == 1
    assert "върви задача" in capsys.readouterr().err
    assert store.user_by_email("ana@example.com") is not None
