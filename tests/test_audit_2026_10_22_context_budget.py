"""Одит 2026-10-22, бюджетът на контекста: компактиране → Anthropic, кешираната
история над тавана и текстовият режим на под-агентите.

Всичко е с фалшив модел (без ключове): мери се какво точно тръгва към
доставчика, не какво би отговорил той.
"""
from __future__ import annotations

import json
import types

import pytest

from genesis_agent import agents
from genesis_agent import brain as brain_mod
from genesis_agent.brain import Brain
from genesis_agent.budget import cache_history, clip_for_context


def _read_round(i: int, content: str) -> list[dict]:
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "READ_FILE", "arguments": json.dumps({"path": f"f{i}.py"})}}]},
        {"role": "tool", "tool_call_id": f"c{i}", "content": content},
    ]


# ── 1. След компактиране Anthropic губеше пазените сурово съобщения ──────────

def test_after_compaction_anthropic_still_gets_the_kept_tail_and_the_final_answer(
        monkeypatch) -> None:
    """Ход с 8 рунда READ_FILE + финален отговор, после компактирането на
    терминала (16/10): опашката започва с assistant(tool_calls). Преводът за
    Anthropic махаше водещия assistant, осиротяваше резултатите му и така
    каскадно — до Claude стигаше само новият въпрос (1 съобщение)."""
    final = "Поправих auth.py: проверката на паролата вече сравнява хеша."
    history: list[dict] = [{"role": "system", "content": "SYS"},
                           {"role": "user", "content": "Поправи login бъга в auth.py"}]
    for i in range(8):
        history += _read_round(i, f"съдържание на f{i}.py: def f{i}(): ...")
    history.append({"role": "assistant", "content": final})

    class _Summarizer:
        def __init__(self, *a, **kw) -> None:
            pass

        def complete(self, messages, **kw):
            return types.SimpleNamespace(raw_text="Резюме: поправка на login, 8 стъпки.",
                                         code="", usage=None, tool_calls=None)

    monkeypatch.setattr(brain_mod, "Brain", _Summarizer)
    compacted = list(Brain.compact_chat_history(history, threshold=16, keep_recent=10))
    assert compacted[2]["role"] == "assistant" and compacted[2].get("tool_calls")
    question = "Добре, а какво точно смени в auth.py?"
    compacted.append({"role": "user", "content": question})

    system, msgs = Brain._to_anthropic_messages(compacted)
    assert "Резюме: поправка на login" in system
    assert msgs[0]["role"] == "user"
    assert msgs[-1] == {"role": "user", "content": question}
    texts = [b.get("text") for m in msgs if m["role"] == "assistant"
             for b in m["content"] if isinstance(b, dict)]
    assert final in texts
    used = {b["id"] for m in msgs if m["role"] == "assistant"
            for b in m["content"] if b.get("type") == "tool_use"}
    results = {b["tool_use_id"] for m in msgs if isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"}
    assert used and results == used   # всеки резултат със своето извикване
    assert len(msgs) >= 10


# ── 2. cache_history прескачаше с цяло парче и пращаше само [system, задача] ──

def _rounds(n: int, size: int) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": "SYS"},
                       {"role": "user", "content": "Поправи бъга в app.py"}]
    for i in range(n):
        out += _read_round(i, clip_for_context(f"съдържание {i} " + "я" * size))
    return out


def _parallel(n: int, size: int) -> list[dict]:
    """Един рунд с n паралелни четения — сам над тавана."""
    out = _rounds(0, 0)
    out.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": f"p{i}", "type": "function",
         "function": {"name": "READ_FILE", "arguments": json.dumps({"path": f"p{i}.py"})}}
        for i in range(n)]})
    out += [{"role": "tool", "tool_call_id": f"p{i}",
             "content": clip_for_context(f"съдържание {i} " + "я" * size)} for i in range(n)]
    return out


@pytest.mark.parametrize("msgs", [_rounds(4, 50_000), _rounds(4, 38_000), _rounds(10, 12_500),
                                  _parallel(4, 50_000)], ids=["4x40K", "4x38K", "10x12.5K", "parallel"])
def test_cache_history_over_the_ceiling_keeps_the_latest_call_and_its_result(msgs) -> None:
    """Мерено 2026-10-22 с таваните по подразбиране (120 000, парче 20): 4 големи
    четения (по 40K след clip) или 10 рунда по 12.5K → тръгваха само
    ['system', 'user'], моделът не виждаше собственото си последно извикване."""
    out = cache_history(msgs, max_chars=120_000, chunk=20)
    assert [m["content"] for m in out[:2]] == ["SYS", "Поправи бъга в app.py"]
    last_call = max(i for i, m in enumerate(msgs) if m.get("tool_calls"))
    assert msgs[last_call] in out
    ids = [tc["id"] for tc in msgs[last_call]["tool_calls"]]
    assert [m["tool_call_id"] for m in out if m.get("role") == "tool"][-len(ids):] == ids
    assert sum(len(str(m.get("content") or "")) for m in out) <= 120_000


# ── 3. Текстовият режим на под-агентите: стари резултати несвити, задачата губена ──

def test_text_mode_subagent_results_are_shrunk_and_the_task_stays(monkeypatch) -> None:
    """agents._loop в текстов режим слагаше резултатите като user „[резултати]“:
    budget_history не ги познаваше като резултати (без свиване) — мерено
    2026-10-22: рунд 10 → 263 242 знака, рунд 15 → 399 883 и без задачата."""
    sent: list[list[dict]] = []

    def fake_call(self, provider, model, messages, tools=None):
        sent.append(messages)
        return "[READ_FILE: big.log]", None

    monkeypatch.setattr(brain_mod.Brain, "_call", fake_call)
    monkeypatch.setattr(brain_mod, "_local_available", lambda m: False)
    monkeypatch.setattr("genesis_agent.provider_stats.deprioritize_flaky", lambda c: c)
    monkeypatch.setattr(brain_mod, "_EXHAUSTED", {})
    monkeypatch.setattr(brain_mod, "_load_chain", lambda: [
        {"provider": "groq", "model": "m", "size_b": 120, "supports_tools": False}])

    calls = [0]

    def parse(text):
        calls[0] += 1
        return [f"[READ_FILE big.log] ред {calls[0]}\n" + "лог ред с кирилица\n" * 1500]

    gs = types.SimpleNamespace(_cancelled=lambda: False, parse_and_execute_tools=parse,
                               dispatch_tool_call=lambda *a: "x")
    agent = agents.Agent(name="t", description="d", prompt="P", tools=frozenset({"READ_FILE"}))
    task = "ЗАДАЧАТА: намери защо пада login"
    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": task}]
    agents._loop(agent, messages, Brain(use_local=False).complete, 15, gs)

    assert len(sent) == 15

    def chars(ms: list[dict]) -> int:
        return sum(len(str(m.get("content") or "")) for m in ms)

    one = 28_600
    # 2 пресни резултата цели + по-старите до STALE_TOOL_RESULT_MAX_CHARS.
    assert chars(sent[9]) < 3 * one, chars(sent[9])
    assert chars(sent[14]) < 3 * one, chars(sent[14])
    assert all(any(task in str(m.get("content")) for m in req) for req in sent)
