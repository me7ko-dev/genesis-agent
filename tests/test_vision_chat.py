"""Картинките в чата (2026-10-15): `@shot.png`, `/image`, пуснат с мишката път,
`genesis -p @img.png`, инструментът VIEW_IMAGE, снимките от проверката в
браузър и `send` с "images" от телефона/Desktop. Фалшиви модели, без мрежа."""
from __future__ import annotations

import io
import json
from collections import deque

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import chat_commands, headless, mentions, plan_mode, sandbox, vision
from tests.test_vision import png


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(gta, "console", gta.console)
    monkeypatch.setattr(gta, "WORKSPACE", tmp_path)
    monkeypatch.setattr(gs, "_WORKSPACE", tmp_path)
    monkeypatch.setattr(sandbox, "_POLICY", sandbox._POLICY)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    (tmp_path / "shot.png").write_bytes(png())
    return tmp_path


def _script(monkeypatch, replies):
    it = iter(replies)
    seen: list[list[dict]] = []

    def fake(messages, tools=None):
        seen.append(list(messages))
        return next(it)
    monkeypatch.setattr(gta, "ask_genesis", fake)
    return seen


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _images(messages) -> list[dict]:
    return [p for m in messages if isinstance(m.get("content"), list)
            for p in m["content"] if vision.is_image_part(p)]


# ── @картинка, пуснат път, /image ────────────────────────────────────────────

def test_at_image_becomes_an_image_part_not_a_binary_note(ws) -> None:
    images: list[dict] = []
    text, notes = mentions.expand("защо е празно @shot.png", ws, images)
    assert text == "защо е празно @shot.png"
    assert len(images) == 1 and images[0]["genesis"]["name"] == "shot.png"
    assert notes == ["@shot.png: прикачена картинка (4×3 px, 0 KB)"]


def test_old_callers_still_get_the_binary_note(ws) -> None:
    _text, notes = mentions.expand("@shot.png", ws)
    assert "двоичен" in notes[0]


def test_a_dragged_absolute_path_is_attached_and_a_bare_name_is_not(ws, tmp_path_factory) -> None:
    other = tmp_path_factory.mktemp("desktop")
    (other / "Screen Shot.png").write_bytes(png())
    images: list[dict] = []
    mentions.expand(f"какво е това '{other / 'Screen Shot.png'}'", ws, images)
    assert [p["genesis"]["name"] for p in images] == ["Screen Shot.png"]
    escaped = str(other / "Screen Shot.png").replace(" ", "\\ ")
    images = []
    mentions.expand(f"{escaped} защо", ws, images)
    assert len(images) == 1
    images = []
    # as_uri: file:///tmp/… тук, file:///C:/… на Windows
    mentions.expand((other / "Screen Shot.png").as_uri(), ws, images)
    assert len(images) == 1
    images = []
    mentions.expand("поправи shot.png в CSS-а", ws, images)
    assert images == [], "голо име в текста не е молба да се прати картинката"


def test_at_most_four_images_per_message(ws) -> None:
    for i in range(6):
        (ws / f"s{i}.png").write_bytes(png(2, 2, (i, 0, 0)))
    images: list[dict] = []
    _t, notes = mentions.expand(" ".join(f"@s{i}.png" for i in range(6)), ws, images)
    assert len(images) == 4 and any("най-много 4 картинки" in n for n in notes)


def test_image_command_attaches_and_asks(ws) -> None:
    (ws / "My Shot.png").write_bytes(png())
    said: list[str] = []
    res = chat_commands.handle("/image My Shot.png защо бутонът е сив?", messages=[], workspace=ws,
                               out=said.append, ask=lambda q: "")
    assert res is not None and res.prompt == "защо бутонът е сив?"
    assert res.images and res.images[0]["genesis"]["name"] == "My Shot.png"
    res = chat_commands.handle("/image shot.png", messages=[], workspace=ws,
                               out=said.append, ask=lambda q: "")
    assert res is not None and res.prompt == chat_commands.IMAGE_QUESTION
    res = chat_commands.handle("/image липсва.png", messages=[], workspace=ws,
                               out=said.append, ask=lambda q: "")
    assert res is not None and res.prompt is None and "не съществува" in said[-1]


def test_chat_main_loop_sends_at_image_and_image_command(ws, monkeypatch) -> None:
    from genesis_agent import brain, chat_input, model_check, workspace_memory
    monkeypatch.setattr(brain, "any_model_reachable", lambda: True, raising=False)
    monkeypatch.setattr(gta, "print_minimal_banner", lambda: None)
    monkeypatch.setattr(gta, "show_status_bar", lambda: None)
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("system", ""))
    monkeypatch.setattr(model_check, "needs_check", lambda: False)
    monkeypatch.setattr(workspace_memory, "auto_capture", lambda convo: {})
    lines = iter(["какво има на @shot.png", "/image shot.png а сега?", "без картинка", "exit"])
    monkeypatch.setattr(chat_input, "read_message", lambda read: next(lines))
    turns: list[tuple[str, list]] = []

    def run_turn(messages, text, ui, images=None):
        turns.append((text, images))
        return messages
    monkeypatch.setattr(gta, "run_turn", run_turn)
    monkeypatch.setattr(gta.sys, "argv", ["genesis"])
    gta.main()
    assert turns[0][0] == "какво има на @shot.png" and len(turns[0][1]) == 1
    assert turns[1][0] == "а сега?" and len(turns[1][1]) == 1
    assert turns[2] == ("без картинка", None), "без картинки — старият подпис"


def test_run_turn_puts_the_image_in_the_operator_message(ws, monkeypatch) -> None:
    seen = _script(monkeypatch, [("Червен квадрат.", None)])
    part = vision.load_image("shot.png", workspace=ws)
    messages = gta.run_turn(deque([{"role": "system", "content": "s"}]), "какво е?", gta.TurnUI(),
                            images=[part])
    user = seen[0][-1]
    assert user["role"] == "user" and _images([user]) == [part]
    assert vision.text_of(user["content"]).startswith("какво е?")
    assert list(messages)[-1] == {"role": "assistant", "content": "Червен квадрат."}


def test_genesis_p_with_at_image(ws, monkeypatch, capsys) -> None:
    seen = _script(monkeypatch, [("Червено.", None)])
    assert headless.run(["какъв", "цвят", "е", "@shot.png?", "--cwd", str(ws)]) == 0
    assert capsys.readouterr().out == "Червено.\n"
    assert len(_images(seen[0])) == 1


# ── VIEW_IMAGE ───────────────────────────────────────────────────────────────

def test_view_image_queues_the_picture_for_the_next_request(ws) -> None:
    result = gs.dispatch_tool_call("VIEW_IMAGE", {"path": "shot.png"})
    assert result.startswith("[VIEW_IMAGE: ") and "] ✓ shot.png (4×3 px" in result
    assert "следващото съобщение" in result
    assert [p["genesis"]["name"] for p in vision.take_pending()] == ["shot.png"]


def test_view_image_text_tag(ws) -> None:
    out = gs.parse_and_execute_tools("[VIEW_IMAGE: shot.png]")
    assert "✓ shot.png" in out[0] and len(vision.take_pending()) == 1


def test_view_image_refuses_a_secret_path_and_a_non_image(ws, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    (ws / ".ssh").mkdir()
    (ws / ".ssh" / "key.png").write_bytes(png())
    assert "SANDBOX DENIED" in gs.dispatch_tool_call("VIEW_IMAGE", {"path": str(ws / ".ssh" / "key.png")})
    (ws / "notes.png").write_text("не съм картинка", encoding="utf-8")
    assert "не е PNG" in gs.dispatch_tool_call("VIEW_IMAGE", {"path": "notes.png"})
    assert vision.take_pending() == []


def test_plan_mode_allows_view_image(ws) -> None:
    plan_mode.set_active(True)
    try:
        assert "✓" in gs.dispatch_tool_call("VIEW_IMAGE", {"path": "shot.png"})
        assert "Режим план" in gs.dispatch_tool_call("WRITE_FILE", {"path": "x.txt", "content": "x"})
    finally:
        plan_mode.set_active(False)
    assert "VIEW_IMAGE" in plan_mode.READ_ONLY_TOOLS


def test_the_viewed_image_reaches_the_next_request_after_the_results(ws, monkeypatch) -> None:
    seen = _script(monkeypatch, [("", [_call("VIEW_IMAGE", {"path": "shot.png"})]),
                                 ("Виждам червено.", None)])
    gta.run_turn(deque([{"role": "system", "content": "s"}]), "погледни shot.png", gta.TurnUI())
    second = seen[1]
    assert second[-2]["role"] == "tool" and "✓ shot.png" in second[-2]["content"]
    assert vision.is_attachment(second[-1]) and len(_images([second[-1]])) == 1


def test_the_tool_schema_is_offered_only_when_someone_can_see(monkeypatch) -> None:
    monkeypatch.setattr(vision, "chain_sees", lambda extra=None: False)
    assert "VIEW_IMAGE" not in {t["function"]["name"] for t in gta._turn_tools()}
    monkeypatch.setattr(vision, "chain_sees", lambda extra=None: True)
    assert "VIEW_IMAGE" in {t["function"]["name"] for t in gta._turn_tools()}


def test_chain_sees_needs_a_key(monkeypatch) -> None:
    from genesis_agent import brain
    chain = [{"provider": "openrouter", "model": "google/gemma-4-31b-it:free", "size_b": 31}]
    monkeypatch.setattr(brain, "_load_chain", lambda: chain)
    monkeypatch.setattr(brain, "_load_coding_chain", list)
    monkeypatch.setattr(brain, "_load_keys", dict)
    assert vision.chain_sees() is False
    monkeypatch.setattr(vision, "_sees_cache", None)
    monkeypatch.setattr(brain, "_load_keys", lambda: {"OPENROUTER_API_KEY": "k"})
    assert vision.chain_sees() is True


# ── снимките от проверката в браузър ─────────────────────────────────────────

def test_page_check_attaches_its_shots_when_the_model_can_see(tmp_path) -> None:
    from genesis_agent import page_check as pc
    (tmp_path / "index.html").write_text('<link rel="stylesheet" href="s.css"><h1>x</h1>',
                                         encoding="utf-8")

    def runner(page, shots=None):
        assert shots is not None
        (shots / "desktop-light.png").write_bytes(png(8, 6))
        (shots / "mobile-light.png").write_bytes(png(3, 6))
        (shots / "desktop-dark.png").write_bytes(png(8, 6, (0, 0, 0)))
        return ["компютър: контраст 1.65:1"], ""
    fc = pc.FinalCheck(runner=runner, shots=True)
    fc.observe(f"[WRITE_FILE: {tmp_path / 'index.html'}] ✓ записани 10 символа")
    note, _line = fc.check()
    assert "1.65:1" in note
    assert [p["genesis"]["name"] for p in fc.images] == ["index.html desktop-light.png",
                                                         "index.html mobile-light.png"]


def test_page_check_without_vision_runs_as_before(tmp_path) -> None:
    from genesis_agent import page_check as pc
    (tmp_path / "index.html").write_text('<link rel="stylesheet" href="s.css"><h1>x</h1>',
                                         encoding="utf-8")
    fc = pc.FinalCheck(runner=lambda page: (["находка"], ""))
    fc.observe(f"[WRITE_FILE: {tmp_path / 'index.html'}] ✓ записани 10 символа")
    assert "находка" in fc.check()[0] and fc.images == []
