"""Паролата в облака (2026-11-15): смяна от „Сметка“ и `reset-password` на
оператора. Преди това човек, забравил паролата, нямаше път обратно, а
откраднатата бисквитка оставаше валидна, каквото и да направи собственикът."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from cloud.web import server as web
from cloud.web.store import Store
from tests.test_cloud_web import PASSWORD, Client, FakeRunner

NEW = "a-brand-new-passphrase"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    runner = FakeRunner()
    app = web.App(store, tmp_path / "jobs", runner=runner, secure_cookie=False)
    srv = web.make_server(app, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield store, lambda: Client(srv.server_address[1])
    runner.gate.set()
    srv.shutdown()
    srv.server_close()
    app.pool.shutdown(wait=True)


def _change(cl: Client, old: str, new: str = NEW) -> tuple[int, dict]:
    return cl.json("POST", "/api/password", {"old": old, "new": new})


# ── смяна от „Сметка“ ─────────────────────────────────────────────────────────

def test_change_keeps_this_session_and_closes_the_others(env) -> None:
    _store, client = env
    here, phone = client(), client()
    assert here.login() == 200 and phone.login() == 200
    code, data = _change(here, PASSWORD)
    assert code == 200 and data == {"ok": True}
    assert here.req("GET", "/api/me")[0] == 200       # от тук сменя — остава вътре
    assert phone.req("GET", "/api/me")[0] == 401      # другото устройство излиза
    assert client().login() == 401                    # старата парола не става
    assert client().login(password=NEW) == 200


def test_wrong_old_password_is_403_and_changes_nothing(env) -> None:
    _store, client = env
    cl = client()
    cl.login()
    code, data = _change(cl, "not-the-password")
    # 403, не 401: 401 в страницата значи „излязъл си“ и показва входа
    assert code == 403 and data["error"] == "грешна стара парола"
    assert cl.req("GET", "/api/me")[0] == 200
    assert client().login() == 200


def test_wrong_old_passwords_count_toward_the_login_lockout(env) -> None:
    """Откраднатата бисквитка не дава безкрайно налучкване на паролата."""
    _store, client = env
    cl = client()
    cl.login()
    for _ in range(web.LOGIN_FAILURES):
        assert _change(cl, "guess-guess-guess")[0] == 403
    assert _change(cl, PASSWORD)[0] == 429            # и вярната вече не минава
    assert client().login() == 429                    # имейлът е заключен и за входа


def test_a_short_new_password_is_400_and_not_counted(env) -> None:
    _store, client = env
    cl = client()
    cl.login()
    for _ in range(web.LOGIN_FAILURES + 2):
        code, data = _change(cl, PASSWORD, "short")
        assert code == 400 and "поне 10 знака" in data["error"]
    assert client().login() == 200                    # не е брояч и не е сменена


def test_change_needs_a_session(env) -> None:
    _store, client = env
    assert _change(client(), PASSWORD)[0] == 401


def test_the_account_page_has_the_form(env) -> None:
    _store, client = env
    cl = client()
    page = cl.req("GET", "/")[2].decode()
    js = cl.req("GET", "/app.js")[2].decode()
    assert 'id="password-form"' in page and 'autocomplete="new-password"' in page
    assert '"/api/password"' in js


# ── reset-password (оператор) ─────────────────────────────────────────────────

def test_reset_password_prints_a_new_one_and_closes_every_session(tmp_path, capsys) -> None:
    store = Store(tmp_path / "web.db")
    uid = store.add_user("ana@example.com", PASSWORD)
    token = store.new_session(uid)
    assert web.main(["reset-password", "Ana@Example.com ", "--data", str(tmp_path)]) == 0
    email, _, password = capsys.readouterr().out.strip().partition("  парола: ")
    assert email == "ana@example.com" and len(password) >= 10
    assert store.check_password(email, password) == uid
    assert store.check_password(email, PASSWORD) is None
    assert store.session_user(token) is None


def test_reset_password_refuses_a_disabled_person(tmp_path, capsys) -> None:
    store = Store(tmp_path / "web.db")
    uid = store.add_user("ana@example.com", PASSWORD)
    store.set_disabled(uid, True)
    assert web.main(["reset-password", "ana@example.com", "--data", str(tmp_path)]) == 1
    assert "спрян" in capsys.readouterr().err
    store.set_disabled(uid, False)
    assert store.check_password("ana@example.com", PASSWORD) == uid   # не е пипана


def test_reset_password_refuses_a_forgotten_person(tmp_path, capsys) -> None:
    store = Store(tmp_path / "web.db")
    uid = store.add_user("ana@example.com", PASSWORD)
    assert store.forget(uid) == ""
    assert web.main(["reset-password", f"deleted-{uid}@invalid", "--data", str(tmp_path)]) == 1
    assert "изтрит" in capsys.readouterr().err
    assert web.main(["reset-password", "nobody@example.com", "--data", str(tmp_path)]) == 1


def test_set_password_keeps_the_signup_rule(tmp_path: Path) -> None:
    store = Store(tmp_path / "web.db")
    uid = store.add_user("ana@example.com", PASSWORD)
    with pytest.raises(ValueError, match="поне 10 знака"):
        store.set_password(uid, "123456789")
    with pytest.raises(ValueError, match="няма такъв човек"):
        store.set_password(uid + 99, NEW)
