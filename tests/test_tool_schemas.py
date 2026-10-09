"""genesis_agent.tool_schemas — structural invariants for the native
function-calling schemas. MISSION_TOOLS in particular must stay read-only +
USE_SKILL only: a mission produces a Python skill as its output, it must not
gain side-effecting tools (RUN_CMD/WRITE_FILE/DELEGATE/browser) through the
tool-composition path (design note in the module, 2026-07-25)."""
from __future__ import annotations

from genesis_agent import tool_schemas


def _names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}


def test_full_tools_have_no_duplicate_names() -> None:
    names = [t["function"]["name"] for t in tool_schemas.FULL_TOOLS]
    assert len(names) == len(set(names))


def test_every_tool_has_valid_openai_function_shape() -> None:
    for t in tool_schemas.FULL_TOOLS:
        assert t["type"] == "function"
        fn = t["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        for required in params.get("required", []):
            assert required in params["properties"]


def test_readonly_tools_is_exactly_the_readonly_set() -> None:
    assert _names(tool_schemas.READONLY_TOOLS) == {"READ_FILE", "WEB_SEARCH", "RESEARCH", "LIST_DIR", "GLOB"}


def test_mission_tools_is_readonly_plus_use_skill() -> None:
    assert _names(tool_schemas.MISSION_TOOLS) == _names(tool_schemas.READONLY_TOOLS) | {"USE_SKILL"}


def _reload_with_playwright(monkeypatch, present: bool):
    import importlib
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: (object() if present else None)
                        if name == "playwright" else real(name, *a))
    return importlib.reload(tool_schemas)


def test_browser_tools_are_not_sent_without_playwright(monkeypatch) -> None:
    """Иначе ~1100 знака на обръщение отиват за инструменти, които могат
    само да върнат грешка."""
    try:
        mod = _reload_with_playwright(monkeypatch, present=False)
        assert _names(mod.FULL_TOOLS).isdisjoint(mod._BROWSER_TOOLS)
        assert "RUN_CMD" in _names(mod.FULL_TOOLS)
    finally:
        monkeypatch.undo()
        import importlib
        importlib.reload(tool_schemas)


def test_the_shipped_prompt_loses_only_the_browser_paragraph(monkeypatch) -> None:
    """Проверено срещу истинския config.yaml — регексът е вързан за текста му."""
    import yaml

    from genesis_agent.paths import CONFIG_PATH
    prompt = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["system_prompt"]
    monkeypatch.setattr(tool_schemas, "browser_available", lambda: False)
    out = tool_schemas.fit_system_prompt(prompt)
    assert "BROWSER SAFETY" in prompt and "BROWSER SAFETY" not in out
    assert "YOUR JOB" in out and "YOUR BODY" in out
    assert len(prompt) - len(out) < 1000
    monkeypatch.setattr(tool_schemas, "browser_available", lambda: True)
    assert tool_schemas.fit_system_prompt(prompt) == prompt


def test_browser_tools_come_back_with_playwright(monkeypatch) -> None:
    try:
        mod = _reload_with_playwright(monkeypatch, present=True)
        assert mod._BROWSER_TOOLS <= _names(mod.FULL_TOOLS)
    finally:
        monkeypatch.undo()
        import importlib
        importlib.reload(tool_schemas)


def test_mission_tools_excludes_side_effecting_tools() -> None:
    forbidden = {"RUN_CMD", "WRITE_FILE", "DELEGATE", "BROWSE",
                 "BROWSER_CLICK", "BROWSER_TYPE", "BROWSER_READ"}
    assert _names(tool_schemas.MISSION_TOOLS).isdisjoint(forbidden)


# ── load_tool_arguments ─────────────────────────────────────────────────────

def test_valid_arguments_are_parsed_as_they_are() -> None:
    raw = r'{"path": "a.py", "content": "x = \"\\d\"\n"}'
    assert tool_schemas.load_tool_arguments(raw) == {"path": "a.py", "content": 'x = "\\d"\n'}


def test_invalid_regex_escapes_become_literal_backslashes() -> None:
    r"""bench faktura-excel: `\d`, `\s`, `\.` в код с регекси — json.loads пада,
    а callers-ите даваха `{}` и WRITE_FILE оставаше без път."""
    got = tool_schemas.load_tool_arguments(r'{"path": "e.py", "content": "r\"\d+\s*\.\"\n"}')
    assert got == {"path": "e.py", "content": 'r"\\d+\\s*\\."\n'}


def test_unrepairable_arguments_still_raise() -> None:
    import json

    import pytest
    with pytest.raises(json.JSONDecodeError):
        tool_schemas.load_tool_arguments('{"path": "a.py", "content": ')


def test_empty_arguments_are_an_empty_dict_but_a_non_object_is_an_error() -> None:
    import json

    import pytest
    assert tool_schemas.load_tool_arguments(None) == {}
    assert tool_schemas.load_tool_arguments({"path": "x"}) == {"path": "x"}
    # Списък не е „без аргументи“: с {} TODO_WRITE триеше списъка (одит 2026-10-09).
    with pytest.raises(json.JSONDecodeError):
        tool_schemas.load_tool_arguments("[1, 2]")


def test_the_schemas_stay_small_and_keep_their_rules() -> None:
    """Схемите се плащат на всяко обръщение. 2026-10-09: 9 400 знака JSON след
    петте нови инструмента (+21% на заявката) → 7 466 след съкращаване на
    описанията. Таванът пази от тихо надуване; правилата, които движат
    поведението, трябва да останат."""
    import json
    tools = [t for t in tool_schemas.FULL_TOOLS
             if t["function"]["name"] not in tool_schemas._BROWSER_TOOLS]
    assert len(json.dumps(tools, ensure_ascii=False)) <= 7600
    desc = {t["function"]["name"]: t["function"]["description"] for t in tools}
    assert "once" in desc["EDIT_FILE"] and "WRITE_FILE" in desc["EDIT_FILE"]
    assert "read" in desc["WRITE_FILE"] and "EDIT_FILE" in desc["WRITE_FILE"]
    assert "in_progress" in desc["TODO_WRITE"] and "3+" in desc["TODO_WRITE"]
    assert "driver_code" in desc["USE_SKILL"]
    assert "why" in desc["REMEMBER"] and "preference" in desc["REMEMBER"]
    assert "BEFORE" in desc["ASK_USER"]
    assert "BG_KILL" in json.dumps(next(t for t in tools if t["function"]["name"] == "RUN_CMD"))


def test_replace_all_as_the_string_false_is_false(tmp_path, monkeypatch) -> None:
    # Модел, който праща булевите като низове: „false“ заменяше ВСИЧКИ
    # съвпадения вместо отказа „среща се 2 пъти“ (одит 2026-10-09).
    import genesis_skills as gs
    monkeypatch.setattr(gs, "_WORKSPACE", tmp_path)
    gs._SEEN_PATHS.clear()
    f = tmp_path / "a.py"
    f.write_text("x = 1\nx = 1\n", encoding="utf-8")
    gs.dispatch_tool_call("READ_FILE", {"path": "a.py"})
    gs.dispatch_tool_call("EDIT_FILE", {"path": "a.py", "old": "x = 1", "new": "x = 2",
                                        "replace_all": "false"})
    assert f.read_text(encoding="utf-8") == "x = 1\nx = 1\n"
    gs.dispatch_tool_call("EDIT_FILE", {"path": "a.py", "old": "x = 1", "new": "x = 2",
                                        "replace_all": "true"})
    assert f.read_text(encoding="utf-8") == "x = 2\nx = 2\n"
