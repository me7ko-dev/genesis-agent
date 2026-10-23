"""Одит 2026-10-22, подсистема „cloud-web“ (cloud/web/server.py).

Като в test_cloud_web.py: истински HTTP сървър на случаен порт и фалшив
runner. В теста за таксата runner-ът е истинският launch.run_task — подменена
е само мрежата на задачата (Docker демонът „не отговаря“)."""
from __future__ import annotations

import http.client
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from test_cloud_web import PASSWORD, Client, FakeRunner

from cloud.runner import launch
from cloud.web import billing
from cloud.web import server as web
from cloud.web.store import MICRO, Store


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    runner = FakeRunner()
    app = web.App(store, tmp_path / "jobs", runner=runner, secure_cookie=False)
    srv = web.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, lambda: Client(srv.server_address[1])
    runner.gate.set()
    srv.shutdown()
    srv.server_close()
    app.pool.shutdown(wait=True)


def _raw(cl: Client, method: str, path: str, body: bytes = b"") -> tuple[Any, Any]:
    c = http.client.HTTPConnection("127.0.0.1", cl.port, timeout=30)
    h = {"Host": f"127.0.0.1:{cl.port}", "Cookie": cl.cookie, "Content-Type": "application/json"}
    c.request(method, path, body=body or None, headers=h)
    try:
        r = c.getresponse()
        return r.status, r.read()
    except (http.client.HTTPException, OSError) as e:
        return "EXC", type(e).__name__
    finally:
        c.close()


def test_a_turn_whose_container_never_started_is_not_charged_or_held(tmp_path, monkeypatch) -> None:
    # Docker демонът не отговаря: run_task се връща преди жетона и контейнера.
    monkeypatch.setattr(launch, "open_task_network", lambda network, helper:
                        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
    usage = tmp_path / "usage.jsonl"
    usage.write_text("")
    store = Store(tmp_path / "web.db")
    uid = store.add_user("ana@example.com", PASSWORD)
    store.add_entry(uid, "admin:start", "grant", 100 * MICRO)
    gateway = launch.Gateway("s" * 40, usage, ("GROQ_API_KEY",))
    app = web.App(store, tmp_path / "jobs", runner=launch.run_task, secure_cookie=False,
                  gateway=gateway, prices=billing.load_prices(), recover=False)
    user = {"id": uid, "email": "ana@example.com"}
    chat = store.new_chat(uid, "x")
    try:
        for i in range(3):
            # Преди поправката вторият ход беше 402 („… кредита са задържани“).
            turn_id = app.start_turn(user, chat, f"задача {i}")
            for _ in range(500):
                if not store.busy(user_id=uid):
                    break
                time.sleep(0.01)
            turn = store.turn(uid, turn_id)
            assert turn is not None and turn["status"] == "failed"
            assert store.balance(uid) == 100 * MICRO       # без таксата на ход
            assert "docker.sock" not in turn["error"]      # без пътя на сокета
        assert app.held(uid) == 0
        assert store.unsettled() == []
    finally:
        app.pool.shutdown(wait=True)


def test_two_downloads_from_one_chat_at_once_both_work(env) -> None:
    app, client = env
    cl = client()
    cl.login()
    _, chat = cl.json("POST", "/api/chats", {"title": "x"})
    _, data = cl.json("POST", f"/api/chats/{chat['id']}/turns", {"text": "hi"})
    cl.wait(data["turn_id"])
    # Първото сваляне още трае (голям файл, бавна връзка).
    assert app.begin_download(chat["id"]) is True
    code, _, body = cl.req("GET", f"/api/chats/{chat['id']}/files/out/result.csv")
    assert code == 200 and body == b"n,sq\n1,1\n2,4\n"
    # Нов ход чака свалянето.
    code, _ = cl.json("POST", f"/api/chats/{chat['id']}/turns", {"text": "пак"})
    assert code == 409
    app.end_download(chat["id"])
    code, data = cl.json("POST", f"/api/chats/{chat['id']}/turns", {"text": "пак"})
    assert code == 202
    cl.wait(data["turn_id"])


def test_huge_numbers_and_lone_surrogates_get_an_http_answer(env) -> None:
    _, client = env
    cl = client()
    cl.login()
    _, chat = cl.json("POST", "/api/chats", {"title": "x"})
    _, data = cl.json("POST", f"/api/chats/{chat['id']}/turns", {"text": "hi"})
    cl.wait(data["turn_id"])
    assert _raw(cl, "GET", "/api/turns/" + "9" * 20 + "/events")[0] == 404
    assert _raw(cl, "GET", "/api/turns/" + "9" * 5000 + "/events")[0] == 404
    code, body = _raw(cl, "GET", f"/api/turns/{data['turn_id']}/events?after=" + "9" * 20)
    assert code == 200, body
    # Текст, отрязан посред емоджи: половин двойка в JSON.
    assert _raw(cl, "POST", "/api/chats", b'{"title": "\\ud800 emoji"}')[0] == 400
    assert _raw(cl, "POST", f"/api/chats/{chat['id']}/turns", b'{"text": "\\ud83d"}')[0] == 400
    assert _raw(cl, "POST", "/api/login", b'{"email": "\\ud800@x", "password": "x"}')[0] == 400
    # Цяла двойка (истинско емоджи) минава.
    assert _raw(cl, "POST", "/api/chats", b'{"title": "\\ud83d\\ude00"}')[0] == 201


@pytest.mark.parametrize("other", ['consent={"ads":false}', "theme=dark mode"])
def test_another_cookie_on_the_domain_does_not_hide_the_session(env, other) -> None:
    _, client = env
    cl = client()
    assert cl.login() == 200
    session = cl.cookie
    cl.cookie = f"{other}; {session}; after=1"
    assert cl.req("GET", "/api/me")[0] == 200
    # Две с името на сесията (подхвърлена с Path=/api идва първа): печели
    # последната, както при SimpleCookie преди поправката.
    cl.cookie = f"{other}; genesis_session=chuzhda; {session}"
    assert cl.req("GET", "/api/me")[0] == 200


def test_admin_limits_prints_zero_and_refuses_negatives(tmp_path: Path, capsys) -> None:
    s = Store(tmp_path / "web.db")
    uid = s.add_user("ana@example.com", PASSWORD)
    assert web.main(["limits", "--data", str(tmp_path), "ana@example.com",
                     "--turns-per-day", "0", "--max-tokens", "0"]) == 0
    out = capsys.readouterr().out
    assert "ходове на ден 0" in out and "токени на ход 0" in out
    assert web.main(["limits", "--data", str(tmp_path), "ana@example.com",
                     "--turns-per-day", "-1"]) == 2
    assert web.main(["limits", "--data", str(tmp_path), "ana@example.com",
                     "--max-tokens", "-5"]) == 2
    acc = s.account(uid)
    assert (acc["turns_per_day"], acc["max_tokens_per_turn"]) == (0, 0)
