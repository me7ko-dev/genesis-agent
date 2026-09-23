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
