"""/context (2026-10-10): с какво е пълен контекстът — промптът по раздели,
инструментите, разговорът. Преди операторът виждаше само общото число в
статус реда."""
from __future__ import annotations

from collections import deque

from genesis_agent import chat_commands, context_usage


def _tool(name: str, desc: str = "x") -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": {}}}}


SYSTEM = ("Ти си Genesis.\n" + "правило\n" * 50
          + "## СРЕДАТА (реални пътища — НЕ ги отгатвай)\nработна папка: /w\n"
          + "## ИНСТРУКЦИИ ЗА ПРОЕКТА (от оператора)\n### CLAUDE.md\n# Проект\n"
          + "## What this is\nтерминален агент\n## ПРАВИЛА\nruff\n## API\nREST\n"
          + "## Под-агенти (от .genesis/agents/)\n- reviewer\n")


def test_system_prompt_is_split_by_genesis_sections_only() -> None:
    parts = dict(context_usage.system_sections(SYSTEM))
    # Одит 2026-10-10: по „главни букви“ „## ПРАВИЛА“ и „## API“ от CLAUDE.md
    # ставаха раздели, а „## Под-агенти“ потъваше в инструкциите.
    assert list(parts) == ["основни правила", "СРЕДАТА", "ИНСТРУКЦИИ ЗА ПРОЕКТА", "Под-агенти"]
    assert parts["ИНСТРУКЦИИ ЗА ПРОЕКТА"] == SYSTEM.index("## Под-агенти") - SYSTEM.index("## ИНСТРУКЦИИ")
    assert sum(parts.values()) == len(SYSTEM)


def test_the_real_prompt_shows_its_sections(tmp_path, monkeypatch) -> None:
    import genesis_terminal_agent as gta
    (tmp_path / ".genesis" / "agents").mkdir(parents=True)
    (tmp_path / ".genesis" / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: преглед\n---\nПрегледай.", encoding="utf-8")
    (tmp_path / "GENESIS.md").write_text("# Проект\n## ПРАВИЛА\nтестове първо\n", encoding="utf-8")
    monkeypatch.setattr(gta, "WORKSPACE", str(tmp_path))
    prompt, _ = gta.build_system_prompt()
    names = [n for n, _ in context_usage.system_sections(prompt)]
    assert {"СРЕДАТА", "ИНСТРУКЦИИ ЗА ПРОЕКТА", "Под-агенти"} <= set(names)
    assert "ПРАВИЛА" not in names


def test_later_system_messages_are_conversation_not_prompt() -> None:
    # Текстов режим: изходите на инструментите са „system“ „[Резултат]:“, а
    # резюмето от /compact — „system“ „## Резюме…“. Броени бяха в промпта.
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "system", "content": "## Резюме на по-ранния разговор:\n" + "р" * 4000},
            {"role": "user", "content": "дай"},
            {"role": "system", "content": "[Резултат]:\n" + "x" * 20000}]
    text = context_usage.report(msgs, [], window=128000)
    prompt_line = next(ln for ln in text.splitlines() if "Системен промпт" in ln)
    assert prompt_line.split()[2] == f"~{len(SYSTEM) // 4}"
    assert "изходи на инструменти 1 ~5.0K (най-голям: [Резултат]" in text
    assert "резюме и бележки 1 ~1.0K" in text


def test_report_names_the_biggest_tool_output_and_splits_the_tools() -> None:
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "оправи теста"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "READ_FILE", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "RUN_CMD", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a" * 8000},
            {"role": "tool", "tool_call_id": "c2", "content": "b" * 400},
            {"role": "assistant", "content": "Готово."}]
    tools = [_tool("READ_FILE"), _tool("RUN_CMD", "y" * 900), _tool("mcp__gh__issues"), _tool("AGENT")]
    text = context_usage.report(msgs, tools, window=128000, last_tokens=4200)
    assert "най-голям: READ_FILE ~2.0K" in text
    assert "изходи на инструменти 2" in text
    assert "вградени 2" in text and "MCP 1" in text and "AGENT" in text
    assert "най-тежки: RUN_CMD" in text
    assert "твоите съобщения 1" in text and "отговори 2" in text
    assert "СРЕДАТА" in text and "от 128K" in text
    assert "4.2K токена" in text
    assert "/compact" not in text


def test_a_full_context_suggests_compact() -> None:
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "я" * 40000}]
    text = context_usage.report(msgs, [], window=12000)
    assert "/compact" in text
    assert "(83%)" in text
    # Пълно от огромен CLAUDE.md — /compact не би помогнал, не се съветва.
    msgs = [{"role": "system", "content": "s" * 40000}, {"role": "user", "content": "я" * 100}]
    assert "/compact" not in context_usage.report(msgs, [], window=12000)


def test_the_chat_command_asks_the_caller_for_tools_and_window(tmp_path) -> None:
    printed: list[str] = []
    msgs = deque([{"role": "system", "content": SYSTEM}], maxlen=10)
    res = chat_commands.handle("/context", messages=msgs, workspace=tmp_path,
                               out=printed.append, ask=lambda q: "",
                               context=lambda: {"tools": [_tool("READ_FILE")], "window": 64000,
                                                "last": 0})
    assert res is not None and res.prompt is None
    assert "Инструменти (1)" in printed[0] and "от 64K" in printed[0]
    # Без сведения от извикващия — пак отговаря, само без инструментите.
    printed.clear()
    chat_commands.handle("/context", messages=msgs, workspace=tmp_path,
                         out=printed.append, ask=lambda q: "")
    assert "без инструменти" in printed[0]


def test_context_from_the_phone_stays_off_the_model(tmp_path) -> None:
    import pytest
    pytest.importorskip("cryptography")
    from genesis_agent import remote_server as rs

    class UI:
        def __init__(self) -> None:
            self.infos: list[str] = []

        def info(self, text: str) -> None:
            self.infos.append(text)

        def warn(self, text: str) -> None:
            self.infos.append(text)

    ui = UI()
    msgs = deque([{"role": "system", "content": SYSTEM}], maxlen=10)
    _, prompt = rs.phone_command("/context", ui, rs.RemoteSession(lambda t, u: None),
                                 messages=msgs, workspace=tmp_path,
                                 context=lambda: {"tools": [_tool("RUN_CMD")], "window": 128000})
    assert prompt is None
    assert any("Инструменти (1)" in t for t in ui.infos)
    assert "/context" in rs.PHONE_HELP
