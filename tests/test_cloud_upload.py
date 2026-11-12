"""cloud/web — качване на файлове в разговора (2026-11-12).

Страницата за ранен достъп обещава „извади сумите от тези PDF фактури“, а уеб
чатът приемаше само текст до 4000 знака: нямаше как файл да стигне до /work.
Тук: истински HTTP сървър, фалшив runner. Проверява се и обратната страна —
кодът в контейнера е чужд, затова качването не пише през връзка, не излиза от
папката и не върви, докато тече ход."""
from __future__ import annotations

import http.client
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from cloud.web import server as web
from cloud.web.store import Store
from tests.test_cloud_web import PASSWORD, Client, FakeRunner

REPO = Path(__file__).resolve().parents[1]
PDF = b"%PDF-1.4\n" + bytes(range(256)) * 40   # двоично, с \x00 и \xff


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "web.db")
    store.add_user("ana@example.com", PASSWORD)
    store.add_user("bob@example.com", PASSWORD)
    runner = FakeRunner()
    app = web.App(store, tmp_path / "jobs", runner=runner, secure_cookie=False)
    srv = web.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, runner, lambda: Client(srv.server_address[1])
    runner.gate.set()
    srv.shutdown()
    srv.server_close()
    app.pool.shutdown(wait=True)


def _upload(cl: Client, chat: str, name: str, data: bytes = PDF, *,
            headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """Сурово тяло, не JSON — както го праща страницата."""
    import json
    c = http.client.HTTPConnection("127.0.0.1", cl.port, timeout=30)
    h = {"Host": f"127.0.0.1:{cl.port}", "Content-Type": "application/octet-stream",
         "Origin": f"http://127.0.0.1:{cl.port}", **(headers or {})}
    if cl.cookie:
        h["Cookie"] = cl.cookie
    c.request("POST", f"/api/chats/{chat}/upload?name={quote(name, safe='')}", body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, json.loads(out or b"{}")


def _chat(cl: Client) -> str:
    assert cl.login() == 200
    return cl.json("POST", "/api/chats", {"title": "фактури"})[1]["id"]


def _ws(app: web.App, chat: str, email: str = "ana@example.com") -> Path:
    uid = app.store.user_by_email(email)["id"]
    return app.workspace(uid, chat)


# ── обещаното: файлът стига до задачата ──────────────────────────────────────

def test_an_uploaded_file_is_in_the_workspace_before_the_first_turn(env) -> None:
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    code, data = _upload(cl, chat, "фактура 12.pdf")
    assert (code, data) == (201, {"path": "фактура 12.pdf", "size": len(PDF)})
    assert (_ws(app, chat) / "фактура 12.pdf").read_bytes() == PDF
    # Вижда се в списъка и се сваля същото, още преди първия ход.
    _, listing = cl.json("GET", f"/api/chats/{chat}/files")
    assert listing["files"] == [{"path": "фактура 12.pdf", "size": len(PDF)}]
    code, _, body = cl.req("GET", f"/api/chats/{chat}/files/{quote('фактура 12.pdf')}")
    assert (code, body) == (200, PDF)


def test_the_task_is_told_which_files_were_uploaded_and_only_once(env) -> None:
    """Моделът не знае, че в папката има нещо ново, ако никой не му каже —
    „извади сумите от тези фактури“ без имена е гадаене."""
    _, runner, client = env
    cl = client()
    chat = _chat(cl)
    assert _upload(cl, chat, "a.pdf")[0] == 201
    assert _upload(cl, chat, "b.xlsx", b"PK\x03\x04")[0] == 201
    _, t = cl.json("POST", f"/api/chats/{chat}/turns", {"text": "извади сумите"})
    cl.wait(t["turn_id"])
    sent = runner.calls[-1][0]
    assert sent.startswith("извади сумите") and "a.pdf" in sent and "b.xlsx" in sent
    # В разговора остава каквото човекът е написал, не бележката към модела.
    _, shown = cl.json("GET", f"/api/chats/{chat}")
    assert shown["turns"][0]["text"] == "извади сумите"
    # Следващият ход не повтаря старите имена.
    _, t = cl.json("POST", f"/api/chats/{chat}/turns", {"text": "сега ги сортирай"})
    cl.wait(t["turn_id"])
    assert runner.calls[-1][0] == "сега ги сортирай"


def test_a_refused_turn_does_not_lose_the_note_about_the_files(env) -> None:
    app, runner, client = env
    cl = client()
    chat = _chat(cl)
    assert _upload(cl, chat, "a.pdf")[0] == 201
    uid = app.store.user_by_email("ana@example.com")["id"]
    app.store.set_account(uid, turns_per_day=0)
    assert cl.json("POST", f"/api/chats/{chat}/turns", {"text": "извади"})[0] == 429
    app.store.set_account(uid, turns_per_day=None)
    _, t = cl.json("POST", f"/api/chats/{chat}/turns", {"text": "извади"})
    cl.wait(t["turn_id"])
    assert "a.pdf" in runner.calls[-1][0]


def test_the_same_name_again_replaces_the_file(env) -> None:
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    assert _upload(cl, chat, "данни.csv", b"a,b\n1,2\n")[0] == 201
    assert _upload(cl, chat, "данни.csv", b"a,b\n3,4\n")[0] == 201
    assert (_ws(app, chat) / "данни.csv").read_bytes() == b"a,b\n3,4\n"
    assert [p.name for p in _ws(app, chat).iterdir()] == ["данни.csv"]   # без остатъци


# ── кой и какво ──────────────────────────────────────────────────────────────

def test_upload_needs_login_and_your_own_chat(env) -> None:
    app, _, client = env
    ana, bob, nobody = client(), client(), client()
    chat = _chat(ana)
    assert bob.login("bob@example.com") == 200
    assert _upload(nobody, chat, "a.pdf")[0] == 401
    assert _upload(bob, chat, "a.pdf")[0] == 404
    assert not _ws(app, chat).exists() or not list(_ws(app, chat).iterdir())


def test_upload_from_a_foreign_origin_or_as_a_form_is_refused(env) -> None:
    """Същата защита като при JSON адресите: чужда страница не качва от името
    на влязъл човек (форма с text/plain минава без preflight)."""
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    assert _upload(cl, chat, "a.pdf", headers={"Origin": "https://evil.example"})[0] == 403
    assert _upload(cl, chat, "a.pdf", headers={"Content-Type": "text/plain"})[0] == 415
    assert _upload(cl, chat, "a.pdf", headers={"Content-Type": "multipart/form-data; boundary=x"})[0] == 415
    assert not list(_ws(app, chat).glob("*"))


@pytest.mark.parametrize("name", [
    "", ".", "..", "../x.pdf", "a/b.pdf", "a\\b.pdf", "/etc/passwd", ".genesis", ".env",
    "C:evil.txt", "a\x00.pdf", "a\n.pdf", "a\x1b[2K.pdf", "\u202efdp.exe", "x" * 300 + ".pdf",
], ids=lambda n: repr(n)[:24])
def test_names_that_leave_the_folder_or_hide_are_refused(env, name: str) -> None:
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    code, data = _upload(cl, chat, name)
    assert code == 400 and data["error"]
    jobs = app.jobs_dir
    assert not [p for p in jobs.rglob("*") if p.is_file()], "нищо не се записва при отказ"


# ── тавани ───────────────────────────────────────────────────────────────────

def test_a_file_over_the_limit_is_413_and_the_client_hears_it(env, monkeypatch) -> None:
    app, _, client = env
    monkeypatch.setattr(web, "MAX_UPLOAD", 1000)
    cl = client()
    chat = _chat(cl)
    # 3 MB: без изчитане на тялото клиентът получава RST вместо отказа (както
    # при /api/login, одит 2026-11-10).
    for _ in range(5):
        code, data = _upload(cl, chat, "a.bin", b"x" * 3_000_000)
        assert code == 413 and "MB" in data["error"]
    assert _upload(cl, chat, "ok.bin", b"x" * 1000)[0] == 201
    assert [p.name for p in _ws(app, chat).iterdir()] == ["ok.bin"]


def test_the_folder_has_a_total_limit(env, monkeypatch) -> None:
    _, _, client = env
    monkeypatch.setattr(web, "MAX_UPLOAD_TOTAL", 2500)
    cl = client()
    chat = _chat(cl)
    assert _upload(cl, chat, "a.bin", b"x" * 1000)[0] == 201
    assert _upload(cl, chat, "b.bin", b"x" * 1000)[0] == 201
    code, data = _upload(cl, chat, "c.bin", b"x" * 1000)
    assert code == 413 and "разговор" in data["error"]
    # Замяната на вече качен файл не се брои два пъти.
    assert _upload(cl, chat, "a.bin", b"y" * 1400)[0] == 201


def test_a_person_has_a_daily_limit_across_chats_and_a_refusal_costs_nothing(env, monkeypatch) -> None:
    """Таванът на разговор сам не пази диска: разговорите са без брой."""
    _, _, client = env
    monkeypatch.setattr(web, "MAX_UPLOAD_DAY", 2500)
    monkeypatch.setattr(web, "MAX_UPLOAD_TOTAL", 1200)
    cl = client()
    one = _chat(cl)
    two = cl.json("POST", "/api/chats", {"title": "втори"})[1]["id"]
    assert _upload(cl, one, "a.bin", b"x" * 1000)[0] == 201
    # Отказано заради тавана на разговора — не се брои към деня.
    assert _upload(cl, one, "b.bin", b"x" * 1000)[0] == 413
    assert _upload(cl, two, "b.bin", b"x" * 1000)[0] == 201
    code, data = _upload(cl, two, "c.txt", b"x" * 600)
    assert code == 429 and "утре" in data["error"]
    assert _upload(cl, two, "c.txt", b"x" * 100)[0] == 201
    # Чужд човек има свой таван.
    bob = client()
    assert bob.login("bob@example.com") == 200
    mine = bob.json("POST", "/api/chats", {"title": "боб"})[1]["id"]
    assert _upload(bob, mine, "a.bin", b"x" * 1000)[0] == 201


def test_two_uploads_into_one_chat_do_not_run_together(env) -> None:
    """Иначе и двете минават проверката „има ли място“, преди някоя да е записала."""
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    uid = app.store.user_by_email("ana@example.com")["id"]
    assert app.begin_upload(uid, chat, 10) == ""
    try:
        code, data = _upload(cl, chat, "a.pdf")
        assert code == 409 and "вече се качва" in data["error"]
    finally:
        app.end_upload(uid, chat, refund=10)
    assert _upload(cl, chat, "a.pdf")[0] == 201


def test_an_empty_file_and_a_body_without_length_are_refused(env) -> None:
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    assert _upload(cl, chat, "empty.txt", b"")[0] == 400
    s = socket.create_connection(("127.0.0.1", cl.port), timeout=10)
    s.sendall((f"POST /api/chats/{chat}/upload?name=a.bin HTTP/1.1\r\nHost: 127.0.0.1:{cl.port}\r\n"
               f"Cookie: {cl.cookie}\r\nContent-Type: application/octet-stream\r\n"
               "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\nabc\r\n0\r\n\r\n").encode())
    reply = s.recv(4096)
    s.close()
    assert reply.startswith((b"HTTP/1.1 411", b"HTTP/1.0 411"))
    assert not list(_ws(app, chat).glob("*"))


def test_a_cut_upload_leaves_nothing_behind(env) -> None:
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    s = socket.create_connection(("127.0.0.1", cl.port), timeout=10)
    s.sendall((f"POST /api/chats/{chat}/upload?name=a.bin HTTP/1.1\r\nHost: 127.0.0.1:{cl.port}\r\n"
               f"Cookie: {cl.cookie}\r\nContent-Type: application/octet-stream\r\n"
               "Content-Length: 5000\r\n\r\n").encode() + b"x" * 100)
    s.close()
    # Сървърът вижда края на връзката и чисти; нов файл със същото име минава
    # (до тогава разговорът е зает с прекъснатото — 409 за няколко милисекунди).
    for _ in range(100):
        code = _upload(cl, chat, "a.bin", b"whole")[0]
        if code != 409:
            break
        time.sleep(0.02)
    assert code == 201
    leftovers = [p for p in app.jobs_dir.rglob("*") if p.is_file() and p.name != "a.bin"]
    assert leftovers == [] and (_ws(app, chat) / "a.bin").read_bytes() == b"whole"


# ── папката е на чужд код ────────────────────────────────────────────────────

def test_no_upload_while_a_turn_is_running_and_no_turn_while_uploading(env) -> None:
    """Както при свалянето: докато контейнерът е жив, той може да подмени име
    с връзка между проверката и записа."""
    app, runner, client = env
    cl = client()
    chat = _chat(cl)
    runner.gate.clear()
    _, t = cl.json("POST", f"/api/chats/{chat}/turns", {"text": "чакай"})
    code, data = _upload(cl, chat, "a.pdf")
    assert code == 409 and "върви" in data["error"]
    runner.gate.set()
    cl.wait(t["turn_id"])
    assert _upload(cl, chat, "a.pdf")[0] == 201
    # Обратното: докато качването държи разговора, ход не започва.
    assert app.begin_download(chat)
    try:
        assert cl.json("POST", f"/api/chats/{chat}/turns", {"text": "x"})[0] == 409
    finally:
        app.end_download(chat)


def test_upload_does_not_write_through_a_link_left_by_the_container(env, tmp_path) -> None:
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    ws = _ws(app, chat)
    ws.mkdir(parents=True)
    victim = tmp_path / "authorized_keys"
    victim.write_bytes(b"old")
    try:
        os.symlink(victim, ws / "report.pdf")
    except OSError:
        pytest.skip("няма права за символни връзки (Windows без Developer Mode)")
    assert _upload(cl, chat, "report.pdf")[0] == 201
    assert victim.read_bytes() == b"old", "качването е писало през връзката"
    assert not (ws / "report.pdf").is_symlink() and (ws / "report.pdf").read_bytes() == PDF


def test_a_folder_with_that_name_is_not_replaced(env) -> None:
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    ws = _ws(app, chat)
    (ws / "out").mkdir(parents=True)
    (ws / "out" / "keep.txt").write_bytes(b"k")
    code, data = _upload(cl, chat, "out")
    assert code == 409 and "папка" in data["error"]
    assert (ws / "out" / "keep.txt").read_bytes() == b"k"


def test_leftover_parts_from_a_crash_are_removed_at_start(env) -> None:
    app, _, client = env
    cl = client()
    chat = _chat(cl)
    assert _upload(cl, chat, "a.pdf")[0] == 201
    part = _ws(app, chat).parent / (web.UPLOAD_PART + "deadbeef")
    part.write_bytes(b"half")
    app.recover()
    assert not part.exists() and (_ws(app, chat) / "a.pdf").exists()


# ── пътят до сървъра и страницата ────────────────────────────────────────────

def test_caddy_lets_an_upload_through_and_keeps_1mb_for_the_rest() -> None:
    """Caddyfile режеше всяко тяло на 1 MB — качването щеше да работи в теста
    и да пада зад истинския прокси."""
    import re
    caddy = (REPO / "cloud" / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    sizes = {m[1]: int(m[2]) for m in re.finditer(
        r"request_body\s+(@\w+)\s*\{\s*max_size\s+(\d+)MB", caddy)}
    assert sizes["@upload"] * 1024 * 1024 >= web.MAX_UPLOAD
    assert sizes["@rest"] == 1
    rx = re.search(r"@upload\s+path_regexp\s+(\S+)", caddy)
    assert rx and re.fullmatch(rx[1], "/api/chats/" + "a" * 32 + "/upload")
    assert not re.fullmatch(rx[1], "/api/login")


def test_the_page_has_a_way_to_attach_files() -> None:
    static = REPO / "cloud" / "web" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'type="file"' in html and "multiple" in html
    assert "/upload?name=" in js and "application/octet-stream" in js
    # Таванът в страницата е този на сървъра — иначе 25 MB пътуват, за да чуят „не“.
    assert web.MAX_UPLOAD == 25 * 1024 * 1024 and "const MAX_UPLOAD = 25 * 1024 * 1024;" in js


def test_the_landing_page_promises_only_what_the_cloud_can_do() -> None:
    """Задачата няма мрежа навън и няма разписание: „GitHub проектът ми“ и
    „всяка сутрин проверява сайта“ бяха обещания, които контейнерът не може
    да изпълни (cloud/README.md: „Направо навън от задача — няма изход“)."""
    page = (REPO / "site" / "public" / "index.html").read_text(encoding="utf-8")
    examples = page.split('<ul class="examples">')[1].split("</ul>")[0]
    for word in ("GitHub", "всяка сутрин", "сайтът ми"):
        assert word not in examples
    assert "PDF фактури" in examples
