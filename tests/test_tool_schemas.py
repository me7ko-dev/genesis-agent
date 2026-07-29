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
    assert _names(tool_schemas.READONLY_TOOLS) == {"READ_FILE", "WEB_SEARCH", "RESEARCH", "LIST_DIR"}


def test_mission_tools_is_readonly_plus_use_skill() -> None:
    assert _names(tool_schemas.MISSION_TOOLS) == _names(tool_schemas.READONLY_TOOLS) | {"USE_SKILL"}


def test_mission_tools_excludes_side_effecting_tools() -> None:
    forbidden = {"RUN_CMD", "WRITE_FILE", "DELEGATE", "BROWSE",
                 "BROWSER_CLICK", "BROWSER_TYPE", "BROWSER_READ"}
    assert _names(tool_schemas.MISSION_TOOLS).isdisjoint(forbidden)
