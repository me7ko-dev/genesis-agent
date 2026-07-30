"""genesis_agent.brain — the OpenAI ↔ Anthropic translation for the paid tier.

Everything else in the chain speaks the OpenAI wire format, so this translation
is the only bespoke protocol code in the project — and the only part that
cannot be smoke-tested by simply having a working key, because a mistake here
produces a 400 from Anthropic rather than a wrong answer.

It is also, conveniently, pure: no network, no key, no SDK. So it is tested
properly here rather than discovered the day someone adds a key.

The invariant these tests protect: whatever goes in as OpenAI-shaped messages
must come out as something Anthropic accepts — a system prompt lifted out of
the message list, tool calls as `tool_use` blocks, tool results as `tool_result`
blocks, no empty content anywhere, and a conversation that starts with `user`.
"""
from __future__ import annotations

import json

from genesis_agent.brain import Brain
from genesis_agent.tool_schemas import REPAIR_TOOLS


def test_system_messages_are_lifted_out_of_the_message_list() -> None:
    system, msgs = Brain._to_anthropic_messages([
        {"role": "system", "content": "ти си агент"},
        {"role": "user", "content": "здрасти"},
    ])
    assert system == "ти си агент"
    assert msgs == [{"role": "user", "content": "здрасти"}]


def test_multiple_system_messages_are_joined() -> None:
    system, _ = Brain._to_anthropic_messages([
        {"role": "system", "content": "първо"},
        {"role": "system", "content": "второ"},
        {"role": "user", "content": "x"},
    ])
    assert system == "първо\n\nвторо"


def test_assistant_tool_calls_become_tool_use_blocks() -> None:
    _, msgs = Brain._to_anthropic_messages([
        {"role": "user", "content": "прочети файла"},
        {"role": "assistant", "content": "ок",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "READ_FILE",
                                      "arguments": json.dumps({"path": "/a.py"})}}]},
    ])
    blocks = msgs[1]["content"]
    assert blocks[0] == {"type": "text", "text": "ок"}
    assert blocks[1] == {"type": "tool_use", "id": "call_1",
                         "name": "READ_FILE", "input": {"path": "/a.py"}}


def test_tool_results_become_tool_result_blocks_keyed_by_id() -> None:
    _, msgs = Brain._to_anthropic_messages([
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_9", "type": "function",
                         "function": {"name": "READ_FILE", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_9", "content": "съдържание"},
    ])
    assert msgs[-1] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_9", "content": "съдържание"}]}


def test_malformed_tool_arguments_degrade_to_empty_input() -> None:
    """A provider that emits invalid JSON must not crash the whole call."""
    _, msgs = Brain._to_anthropic_messages([
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c", "type": "function",
                         "function": {"name": "READ_FILE", "arguments": "{не е json"}}]},
    ])
    assert msgs[1]["content"][0]["input"] == {}


def test_empty_assistant_turn_is_dropped() -> None:
    """Anthropic rejects a message with no content; an empty turn carries none."""
    _, msgs = Brain._to_anthropic_messages([
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "   "},
        {"role": "user", "content": "y"},
    ])
    assert [m["role"] for m in msgs] == ["user", "user"]


def test_empty_tool_result_gets_a_placeholder_not_an_empty_string() -> None:
    _, msgs = Brain._to_anthropic_messages([
        {"role": "user", "content": "x"},
        {"role": "tool", "tool_call_id": "c", "content": ""},
    ])
    assert msgs[-1]["content"][0]["content"] == "(празен резултат)"


def test_conversation_is_forced_to_start_with_user() -> None:
    _, msgs = Brain._to_anthropic_messages([
        {"role": "assistant", "content": "водещ assistant ход"},
        {"role": "user", "content": "истинското начало"},
    ])
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "истинското начало"


def test_no_message_ever_carries_empty_content() -> None:
    """The single most common cause of a 400 from the Messages API."""
    _, msgs = Brain._to_anthropic_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": None},
        {"role": "user", "content": "истинско"},
    ])
    for m in msgs:
        assert m["content"], m


def test_tool_schemas_convert_to_anthropic_shape() -> None:
    converted = Brain._to_anthropic_tools(REPAIR_TOOLS)
    assert len(converted) == len(REPAIR_TOOLS)
    for tool, original in zip(converted, REPAIR_TOOLS, strict=True):
        assert set(tool) == {"name", "description", "input_schema"}
        assert tool["name"] == original["function"]["name"]
        # The schema must survive intact — a dropped `required` turns a typed
        # tool into one the model can call with anything.
        assert tool["input_schema"] == original["function"]["parameters"]


def test_tool_without_parameters_still_gets_a_valid_schema() -> None:
    converted = Brain._to_anthropic_tools([
        {"type": "function", "function": {"name": "BROWSER_READ", "description": "d"}}])
    assert converted[0]["input_schema"] == {"type": "object", "properties": {}}


def test_a_full_repair_round_trip_survives_translation() -> None:
    """The exact shape repo_agent builds: system, task, tool call, tool result."""
    system, msgs = Brain._to_anthropic_messages([
        {"role": "system", "content": "поправи бъга"},
        {"role": "user", "content": "median() бърка при четен брой"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "EDIT_FILE",
                                      "arguments": json.dumps({"path": "s.py", "old": "a",
                                                               "new": "b"})}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "✓ редактиран"},
        {"role": "user", "content": "тестовете още падат"},
    ])
    assert system == "поправи бъга"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "user"]
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[2]["content"][0]["type"] == "tool_result"
