"""Предплатеният кредит в Anthropic (2026-10-22).

Операторът получи $100 кредит за API-то, валиден ~20 дни, и иска Genesis да го
ползва, докато свърши, а после да продължи на безплатната верига без грешки.
Досега: цена се записваше само когато доставчикът я казва сам (`claude` CLI),
таван нямаше, а свършен кредит (402 или 400 „credit balance is too low“)
спираше ключа за 5 минути или изобщо не го спираше — тоест на всеки няколко
минути ходът започваше с провалена платена заявка.

`anthropic` е фалшив модул: в CI няма ключ и нищо не бива да се плаща.
"""
from __future__ import annotations

import contextlib
import json
import sys
import time
import types
from pathlib import Path

import pytest

from genesis_agent import brain

ROOT = Path(__file__).resolve().parent.parent

CREDIT_400 = ("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
              "'message': 'Your credit balance is too low to access the Anthropic API. "
              "Please go to Plans & Billing to upgrade or purchase credits.'}}")
BILLING_402 = ("Error code: 402 - {'type': 'error', 'error': {'type': 'billing_error', "
               "'message': 'Your organization has no remaining credit.'}}")
CREDIT_LINE = "Кредитът в Anthropic свърши — продължавам с безплатните модели"


def spend():
    from genesis_agent import anthropic_spend
    return anthropic_spend


# ── фалшивото SDK ────────────────────────────────────────────────────────────

class Usage:
    def __init__(self, inp=0, out=0, read=0, write=0, w5=None, w1h=None, iterations=None):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = read
        self.cache_creation_input_tokens = write
        self.cache_creation = (None if w5 is None and w1h is None else
                               types.SimpleNamespace(ephemeral_5m_input_tokens=w5 or 0,
                                                     ephemeral_1h_input_tokens=w1h or 0))
        self.iterations = iterations


class _Text:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeSDK:
    """Модулът `anthropic`: записва всяка заявка; `errors` (по ред) се вдигат
    вместо отговор — (beta?, изключение)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.errors: list[tuple[bool | None, Exception]] = []
        self.stop_reason = "end_turn"
        self.usage: object = Usage(1000, 200)
        self.text = "от Claude"
        # Параметри, които (по-старото) SDK не познава → TypeError, като
        # истинското: `stream() got an unexpected keyword argument`.
        self.unknown_kwargs: set[str] = set()
        self.served_model: str | None = None
        self.module = self._build()

    def _build(self) -> types.ModuleType:
        sdk = self
        mod = types.ModuleType("anthropic")

        class APIStatusError(Exception):
            status_code = 500

            def __init__(self, message: str, status_code: int | None = None) -> None:
                super().__init__(message)
                self.message = message
                if status_code is not None:
                    self.status_code = status_code

        class BadRequestError(APIStatusError):
            status_code = 400

        class AuthenticationError(APIStatusError):
            status_code = 401

        class PermissionDeniedError(APIStatusError):
            status_code = 403

        class RateLimitError(APIStatusError):
            status_code = 429

        class APIConnectionError(Exception):
            pass

        class _Messages:
            def __init__(self, beta: bool) -> None:
                self.beta = beta

            def stream(self, **params):
                for name in sorted(sdk.unknown_kwargs & set(params)):
                    raise TypeError(f"Messages.stream() got an unexpected keyword argument '{name}'")
                sdk.calls.append({**params, "_beta": self.beta})
                for i, (beta, err) in enumerate(sdk.errors):
                    if beta is None or beta == self.beta:
                        sdk.errors.pop(i)
                        raise err
                resp = types.SimpleNamespace(stop_reason=sdk.stop_reason, content=[_Text(sdk.text)],
                                             usage=sdk.usage,
                                             model=sdk.served_model or params["model"],
                                             stop_details=None)
                return contextlib.nullcontext(types.SimpleNamespace(get_final_message=lambda: resp))

        class Anthropic:
            def __init__(self, **_kw) -> None:
                self.beta = types.SimpleNamespace(messages=_Messages(True))
                self.messages = _Messages(False)

        for cls in (APIStatusError, BadRequestError, AuthenticationError, PermissionDeniedError,
                    RateLimitError, APIConnectionError):
            setattr(mod, cls.__name__, cls)
        mod.Anthropic = Anthropic  # type: ignore[attr-defined]
        return mod

    def fail(self, kind: str, *, beta: bool | None = None) -> None:
        if kind == "402":
            err = self.module.APIStatusError(BILLING_402, status_code=402)
        elif kind == "400-credit":
            err = self.module.BadRequestError(CREDIT_400)
        elif kind == "400-beta":
            err = self.module.BadRequestError(
                "Error code: 400 - Unexpected value(s) `server-side-fallback-2026-07-01` "
                "for the `anthropic-beta` header.")
        else:
            err = self.module.BadRequestError(
                'Error code: 400 - messages: roles must alternate between "user" and "assistant"')
        self.errors.append((beta, err))


@pytest.fixture
def sdk(monkeypatch) -> FakeSDK:
    fake = FakeSDK()
    monkeypatch.setitem(sys.modules, "anthropic", fake.module)
    return fake


class FakeResp:
    def __init__(self, status: int, body) -> None:
        self.status_code = status
        self._body = body
        self.headers: dict = {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


_KEY_ENVS = ("GROQ_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
             "DEEPSEEK_API_KEY", "HF_TOKEN", "OLLAMA_API_KEY", "COHERE_API_KEY",
             "CEREBRAS_API_KEY", "SAMBANOVA_API_KEY", "TOGETHER_API_KEY", "GEMINI_API_KEY",
             "GOOGLE_CLOUD_PROJECT", "ANTHROPIC_API_KEY")


@pytest.fixture
def chain(monkeypatch, sdk) -> list[str]:
    """Пин на Anthropic отпред + един безплатен модел (groq/free-A) отзад.
    Връща имената на моделите, които безплатният доставчик е получил."""
    monkeypatch.setattr(brain, "_EXHAUSTED", {})
    monkeypatch.setattr(brain, "_KEY_429_MODELS", {})
    monkeypatch.setattr(brain, "_local_available", lambda m: False)
    monkeypatch.setattr("genesis_agent.provider_stats.deprioritize_flaky", lambda c: c)
    monkeypatch.setattr(brain, "_load_chain", lambda: [
        {"provider": "groq", "model": "free-A", "size_b": 120, "supports_tools": True}])
    for name in (*_KEY_ENVS, "GENESIS_QUALITY", "GENESIS_EFFORT", "GENESIS_MODEL_GATEWAY",
                 "GENESIS_ANTHROPIC_MAX_USD", "GENESIS_LOCAL_ONLY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-123456")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-not-real")
    free: list[str] = []

    def post(url, *, headers, json, timeout):
        free.append(json["model"])
        return FakeResp(200, {"choices": [{"message": {"role": "assistant",
                                                       "content": "от безплатния"},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    monkeypatch.setattr(brain, "_post_with_deadline", post)
    return free


def _pinned(model: str = "claude-opus-5-5") -> brain.Brain:
    return brain.Brain(pin_model=("anthropic", model), use_local=False)


HI = [{"role": "user", "content": "здравей"}]


# ── 1. моделите ──────────────────────────────────────────────────────────────

def test_premium_models_are_opus_5_5_xhigh_and_sonnet_5_5_high() -> None:
    paid = [(c["model"], c["effort"]) for c in brain._load_premium_chain()
            if c["provider"] == "anthropic"]
    assert paid == [("claude-opus-5-5", "xhigh"), ("claude-sonnet-5-5", "high")]


def test_without_the_live_list_the_menu_and_the_key_check_offer_the_current_models() -> None:
    import genesis_terminal_agent as gta
    from genesis_agent import setup_wizard
    assert gta.FALLBACKS["anthropic"][:2] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert not {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"} & set(gta.FALLBACKS["anthropic"])
    probe = next(p for p in setup_wizard.PAID_PROVIDERS if p[0] == "ANTHROPIC_API_KEY")
    assert probe[-1] in gta.FALLBACKS["anthropic"]


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
def test_the_current_models_get_adaptive_thinking_and_effort(model) -> None:
    assert brain._supports_adaptive_effort(model)


@pytest.mark.parametrize("model,effort", [("claude-opus-5-5", "xhigh"), ("claude-sonnet-5-5", "high")])
def test_the_request_carries_nothing_the_new_models_reject(sdk, monkeypatch, model, effort) -> None:
    """Opus 5.5 и Sonnet 5.5: thinking не се изключва (400), budget_tokens — 400,
    принудителен tool_choice — 400, prefill (последен ход assistant) — 400, а от
    2026-10-21 temperature/top_p/top_k — 400 на всеки модел."""
    monkeypatch.delenv("GENESIS_EFFORT", raising=False)
    tools = [{"type": "function", "function": {"name": "READ_FILE", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}]
    history = [{"role": "system", "content": "ти си Genesis"},
               {"role": "user", "content": "направи го"},
               {"role": "assistant", "content": "Започвам с файла."}]
    brain.Brain()._call_anthropic("k", model, history, tools, effort)
    sent = sdk.calls[-1]
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & set(sent)
    assert sent["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(sent, default=str)
    assert sent["output_config"] == {"effort": effort}
    assert sent["messages"][-1]["role"] == "user", "без prefill"
    assert sent["messages"][-2]["content"] == [{"type": "text", "text": "Започвам с файла."}]


def test_a_tool_call_left_without_its_result_at_the_end_is_not_sent(sdk) -> None:
    """tool_use без tool_result след него е 400 така или иначе — остава текстът."""
    history = [{"role": "user", "content": "прочети"},
               {"role": "assistant", "content": "Чета.", "tool_calls": [
                   {"id": "c1", "type": "function",
                    "function": {"name": "READ_FILE", "arguments": "{}"}}]}]
    brain.Brain()._call_anthropic("k", "claude-opus-5-5", history, None, "xhigh")
    sent = sdk.calls[-1]["messages"]
    assert sent[-1]["role"] == "user"
    assert all(b.get("type") != "tool_use" for m in sent if isinstance(m["content"], list)
               for b in m["content"])


# ── 2. разходът в долари ──────────────────────────────────────────────────────

def test_the_cost_of_one_response_follows_the_price_table() -> None:
    s = spend()
    # Opus 5.5: вход $4, изход $20, четене от кеша $0.20; запис 5 мин ×1.25, 1 час ×2.
    usage = Usage(inp=1000, out=2000, read=10_000, write=4000, w5=1000, w1h=3000)
    want = (1000 * 4 + 2000 * 20 + 10_000 * 0.20 + 1000 * 4 * 1.25 + 3000 * 4 * 2) / 1e6
    assert s.usage_cost("claude-opus-5-5", usage) == pytest.approx(want)
    assert s.usage_cost("claude-sonnet-5-5", Usage(inp=1_000_000)) == pytest.approx(2.0)
    assert s.usage_cost("claude-opus-5", Usage(read=1_000_000)) == pytest.approx(0.50)
    assert s.usage_cost("claude-fable-5-1", Usage(out=1_000_000)) == pytest.approx(50.0)
    assert s.usage_cost("claude-haiku-5-5", Usage(inp=10_000, out=1000)) == pytest.approx(
        (10_000 * 0.10 + 1000 * 0.50) / 1e6)


def test_an_unknown_model_has_no_cost_rather_than_a_wrong_one() -> None:
    s = spend()
    assert s.usage_cost("claude-future-9", Usage(inp=1000, out=1000)) is None
    # Име по префикс не е същият модел: Opus 5.5 не струва като Opus 5.
    assert s.usage_cost("claude-opus-5-5", Usage(inp=1_000_000)) == pytest.approx(4.0)
    assert s.usage_cost("claude-opus-5", Usage(inp=1_000_000)) == pytest.approx(5.0)


def test_cache_writes_without_a_breakdown_are_counted_at_the_dearer_rate() -> None:
    """Таванът не бива да брои по-малко от сметката."""
    assert spend().usage_cost("claude-sonnet-5-5", Usage(write=1_000_000)) == pytest.approx(4.0)


def test_a_server_side_fallback_bills_each_attempt_at_its_own_models_price() -> None:
    s = spend()
    resp = types.SimpleNamespace(model="claude-opus-5", usage=Usage(
        inp=10, out=10, iterations=[
            {"type": "message", "input_tokens": 1_000_000, "output_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-5",
             "input_tokens": 1_000_000, "output_tokens": 0}]))
    assert s.response_cost("claude-opus-5-5", resp) == pytest.approx(4.0 + 5.0)


def test_every_response_puts_its_cost_in_usage_and_in_the_running_total(sdk) -> None:
    s = spend()
    sdk.usage = Usage(inp=100_000, out=10_000)          # $0.40 + $0.20
    b = brain.Brain()
    b._call_anthropic("k", "claude-opus-5-5", HI, None, "xhigh")
    assert b._last_usage["cost_usd"] == pytest.approx(0.60)
    assert s.total_usd() == pytest.approx(0.60)
    b._call_anthropic("k", "claude-opus-5-5", HI, None, "xhigh")
    assert s.total_usd() == pytest.approx(1.20)
    data = json.loads(s.SPEND_PATH.read_text(encoding="utf-8"))
    assert data["total_usd"] == pytest.approx(1.20) and data["calls"] == 2
    assert not [p for p in s.SPEND_PATH.parent.iterdir() if p.name.endswith(".tmp")]


def test_a_refused_or_cut_response_is_billed_too(sdk) -> None:
    sdk.usage = Usage(inp=100_000, out=0)
    sdk.stop_reason = "refusal"
    with pytest.raises(RuntimeError, match="REFUSAL"):
        brain.Brain()._call_anthropic("k", "claude-opus-5-5", HI, None, "xhigh")
    assert spend().total_usd() == pytest.approx(0.40)


def test_an_unknown_model_adds_nothing_to_the_total(sdk) -> None:
    b = brain.Brain()
    b._call_anthropic("k", "claude-future-9", HI, None, "")
    assert "cost_usd" not in b._last_usage
    assert spend().total_usd() == 0


def test_the_total_survives_into_the_next_session(sdk) -> None:
    s = spend()
    s.add(0.25, "claude-sonnet-5-5")
    s.reset_session()                       # нов процес: паметта е празна, файлът — не
    assert s.total_usd() == pytest.approx(0.25)


def test_the_spend_file_is_never_the_operators_in_tests(tmp_path) -> None:
    path = spend().SPEND_PATH
    assert tmp_path in path.parents
    assert Path.home() / ".genesis" not in path.parents


def test_the_budget_log_gets_the_cost(chain, sdk) -> None:
    from genesis_agent import budget
    sdk.usage = Usage(inp=100_000, out=10_000)
    reply = _pinned().complete(HI)
    assert reply.raw_text == "от Claude"
    rows = [json.loads(line) for line in Path(budget.LOG_PATH).read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["provider"] == "anthropic" and rows[-1]["cost_usd"] == pytest.approx(0.60)


# ── 3. таванът ──────────────────────────────────────────────────────────────

def test_at_the_cap_the_chain_goes_to_the_free_models_with_one_notice(chain, sdk, monkeypatch,
                                                                       capsys) -> None:
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "1")
    spend().add(1.0, "claude-opus-5-5")
    assert _pinned().complete(HI).raw_text == "от безплатния"
    assert _pinned().complete(HI).raw_text == "от безплатния"
    assert sdk.calls == [], "платената заявка не тръгва изобщо"
    out = capsys.readouterr().out
    assert out.count("Anthropic: похарчени $1.00 от $1.00 — минавам на безплатните модели") == 1
    assert "anthropic/claude-opus-5-5" not in out, "без ред „✗ … → следващ“ на всеки ход"


def test_below_the_cap_claude_answers(chain, sdk, monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "95")
    spend().add(94.0, "claude-opus-5-5")
    assert _pinned().complete(HI).raw_text == "от Claude"


def test_the_cap_is_read_from_the_genesis_env_file(chain, sdk, tmp_path) -> None:
    from genesis_agent import paths
    Path(paths.ENV_FILES[-1]).write_text("GENESIS_ANTHROPIC_MAX_USD=0,50\n", encoding="utf-8")
    spend().add(0.5, "claude-opus-5-5")
    assert _pinned().complete(HI).raw_text == "от безплатния"
    assert sdk.calls == []


def test_without_a_cap_there_is_no_limit(chain, sdk) -> None:
    spend().add(500.0, "claude-opus-5-5")
    assert _pinned().complete(HI).raw_text == "от Claude"


# ── 4. кредитът свърши ───────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["402", "400-credit"])
def test_no_credit_moves_the_turn_on_and_never_asks_anthropic_again(chain, sdk, monkeypatch,
                                                                    capsys, kind) -> None:
    sdk.fail(kind)
    assert _pinned().complete(HI).raw_text == "от безплатния"
    assert len(sdk.calls) == 1
    # Следващ ход, нов Brain и час по-късно (не 5-минутният cooldown) — пак без заявка.
    assert _pinned().complete(HI).raw_text == "от безплатния"
    real = time.time
    monkeypatch.setattr(brain.time, "time", lambda: real() + 3600)
    assert _pinned("claude-sonnet-5-5").complete(HI).raw_text == "от безплатния"
    assert len(sdk.calls) == 1
    out = capsys.readouterr().out
    assert out.count(CREDIT_LINE) == 1
    assert "sk-ant-test" not in out


def test_no_credit_after_the_retry_without_the_beta_is_still_a_clean_fallback(chain, sdk,
                                                                              capsys) -> None:
    """Повторът без beta-та беше извън превода на грешките: суровото изключение
    на SDK-то излизаше от complete() и ходът падаше."""
    sdk.fail("400-beta", beta=True)
    sdk.fail("400-credit", beta=False)
    assert _pinned().complete(HI).raw_text == "от безплатния"
    assert [c["_beta"] for c in sdk.calls] == [True, False]
    assert CREDIT_LINE in capsys.readouterr().out


def test_another_400_does_not_pass_for_no_credit(chain, sdk, capsys) -> None:
    sdk.fail("400-other")
    assert _pinned().complete(HI).raw_text == "от безплатния"
    assert _pinned().complete(HI).raw_text == "от Claude"
    assert CREDIT_LINE not in capsys.readouterr().out


# ── 5. видимост ──────────────────────────────────────────────────────────────

def _anthropic_row(monkeypatch, tmp_path):
    from genesis_agent import doctor
    monkeypatch.setattr(doctor, "probes", lambda online=False: [("Anthropic", doctor.check_anthropic)])
    rows = doctor.run_checks(tmp_path)
    assert len(rows) == 1
    return rows[0]


def test_doctor_shows_the_spend_and_the_cap_but_never_the_key(monkeypatch, tmp_path, sdk) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-123456")
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "95")
    spend().add(1.2345, "claude-opus-5-5")
    row = _anthropic_row(monkeypatch, tmp_path)
    assert row.status == "ok"
    assert "$1.23" in row.detail and "$95.00" in row.detail
    assert "sk-ant" not in row.detail + row.fix


def test_doctor_lists_the_anthropic_row() -> None:
    from genesis_agent import doctor
    assert any(fn is doctor.check_anthropic for _, fn in doctor.probes())


def test_doctor_without_a_key_says_it_is_not_used(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    row = _anthropic_row(monkeypatch, tmp_path)
    assert row.status == "skip" and "ANTHROPIC_API_KEY" in row.detail


def test_doctor_says_without_a_cap_and_at_the_cap(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-123456")
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a: object())
    monkeypatch.delenv("GENESIS_ANTHROPIC_MAX_USD", raising=False)
    row = _anthropic_row(monkeypatch, tmp_path)
    assert row.status == "ok" and "без таван" in row.detail
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "2")
    spend().add(2.5, "claude-opus-5-5")
    row = _anthropic_row(monkeypatch, tmp_path)
    assert row.status == "skip" and "таванът е достигнат" in row.detail
    assert "GENESIS_ANTHROPIC_MAX_USD" in row.fix


def test_doctor_names_a_bad_cap_and_a_missing_package(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-123456")
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a: object())
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "сто")
    row = _anthropic_row(monkeypatch, tmp_path)
    assert row.status == "fail" and "сто" in row.detail
    monkeypatch.delenv("GENESIS_ANTHROPIC_MAX_USD")
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a: None)
    row = _anthropic_row(monkeypatch, tmp_path)
    assert row.status == "fail" and "pipx inject genesis-agent anthropic" in row.fix


def test_doctor_says_the_credit_ran_out_this_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-123456")
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a: object())
    spend().mark_credit_gone()
    row = _anthropic_row(monkeypatch, tmp_path)
    assert row.status == "skip" and "кредитът свърши" in row.detail


def test_genesis_budget_shows_the_usd(monkeypatch, capsys) -> None:
    from genesis_agent import budget, cli
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "95")
    budget.record_usage(provider="anthropic", model="claude-opus-5-5", prompt_tokens=100,
                        completion_tokens=10, cost_usd=0.78)
    spend().add(0.78, "claude-opus-5-5")
    assert cli.main(["budget"]) == 0
    out = capsys.readouterr().out
    assert "$0.7800" in out                              # редът на доставчика
    assert "Anthropic (API ключ): похарчени $0.78 от тавана $95.00" in out


# ── 7. ревю 2026-10-22: повреден файл, Windows, модели без цена, старо SDK ────

def _no_nan(text: str):
    def refuse(name):
        raise AssertionError(f"{name} във файла")
    return json.loads(text, parse_constant=refuse)


@pytest.mark.parametrize("body", [b'{"total_usd": NaN}', b'{"total_usd": Infinity}',
                                  b'{"total_usd": 1e400}', b'{"total_usd": -1e9}',
                                  b'{"total_usd": 1, "by_model": {"claude-opus-5-5": NaN}}',
                                  b"[1, 2]", b"garbage{", b"\xff\xfe"])
def test_a_damaged_spend_file_counts_from_zero_and_never_blocks(monkeypatch, caplog, body) -> None:
    """json.loads приема NaN/Infinity: „$nan“ спираше Anthropic завинаги, а
    отрицателен сбор изключваше тавана. Боклук → 0, с debug следа."""
    import logging
    s = spend()
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "95")
    s.SPEND_PATH.parent.mkdir(parents=True, exist_ok=True)
    s.SPEND_PATH.write_bytes(body)
    with caplog.at_level(logging.DEBUG, logger="genesis.anthropic_spend"):
        assert s.total_usd() in (0.0, 1.0)
        assert s.blocked() is None
        total = s.add(0.5, "claude-opus-5-5")
    assert any(r.name == "genesis.anthropic_spend" for r in caplog.records), "без debug следа"
    assert total in (0.5, 1.5)
    data = _no_nan(s.SPEND_PATH.read_text(encoding="utf-8"))
    assert data["total_usd"] == total
    assert all(v >= 0 for v in data["by_model"].values())


def test_a_spend_file_busy_on_windows_is_not_overwritten(monkeypatch) -> None:
    """PermissionError при четене (друг процес прави os.replace, антивирус):
    досега {} → add() записваше само последния отговор и сборът се губеше."""
    s = spend()
    s.add(50.0, "claude-opus-5-5")
    real = Path.read_text
    busy = {"left": 2}

    def read_text(self, *a, **kw):
        if self == s.SPEND_PATH and busy["left"]:
            busy["left"] -= 1
            raise PermissionError(13, "The process cannot access the file")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr("genesis_agent.anthropic_spend.time.sleep", lambda _s: None)
    assert s.add(0.5, "claude-opus-5-5") == pytest.approx(50.5), "кратък повтор"
    busy["left"] = 10**6                                  # зает докрай
    s.add(0.5, "claude-opus-5-5")
    monkeypatch.setattr(Path, "read_text", real)
    assert s.total_usd() == pytest.approx(50.5), "незает файл не се презаписва"


def test_a_failed_replace_leaves_no_temp_file(monkeypatch) -> None:
    s = spend()
    s.add(1.0, "claude-opus-5-5")

    def replace(_src, _dst):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr("genesis_agent.anthropic_spend.os.replace", replace)
    monkeypatch.setattr("genesis_agent.anthropic_spend.time.sleep", lambda _s: None)
    s.add(0.5, "claude-opus-5-5")
    assert not [p.name for p in s.SPEND_PATH.parent.iterdir() if p.name.endswith(".tmp")]
    assert s.total_usd() == pytest.approx(1.0)


def test_dated_ids_from_the_live_list_have_their_price(sdk) -> None:
    """/model предлага живия списък на ключа — `claude-haiku-4-5-20251001`, а
    `resp.model` носи същото име. Без цена таванът не важеше за тях."""
    s = spend()
    assert s.usage_cost("claude-haiku-4-5-20251001", Usage(inp=1_000_000)) == pytest.approx(1.0)
    assert s.usage_cost("claude-fable-5", Usage(read=1_000_000)) == pytest.approx(1.0)
    sdk.served_model = "claude-haiku-4-5-20251001"
    sdk.usage = Usage(inp=100_000, out=10_000)          # $0.10 + $0.05
    b = brain.Brain()
    b._call_anthropic("k", "claude-haiku-4-5-20251001", HI, None, "")
    assert b._last_usage["cost_usd"] == pytest.approx(0.15)
    assert s.total_usd() == pytest.approx(0.15)


def test_haiku_over_100k_input_is_billed_at_the_long_prompt_price() -> None:
    usage = Usage(inp=50_000, read=60_000, out=1000)
    assert spend().usage_cost("claude-haiku-5-5", usage) == pytest.approx(
        (50_000 * 0.50 + 60_000 * 0.05 + 1000 * 2.50) / 1e6)


def test_under_a_cap_a_model_without_a_price_is_not_called(chain, sdk, monkeypatch,
                                                           capsys) -> None:
    """Иначе таванът не го спира: разходът му не влиза в сбора."""
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "95")
    assert _pinned("claude-future-9").complete(HI).raw_text == "от безплатния"
    assert _pinned("claude-future-9").complete(HI).raw_text == "от безплатния"
    assert sdk.calls == []
    assert capsys.readouterr().out.count("няма цена за claude-future-9") == 1
    # Без таван няма какво да пази — моделът се вика.
    monkeypatch.delenv("GENESIS_ANTHROPIC_MAX_USD")
    assert _pinned("claude-future-9").complete(HI).raw_text == "от Claude"


def test_a_bad_cap_is_said_once_in_the_chat(chain, sdk, monkeypatch, capsys) -> None:
    """„95 USD“ досега тихо значеше „без таван“ — казваше го само /doctor."""
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "95 USD")
    assert _pinned().complete(HI).raw_text == "от Claude"   # стената е самият кредит
    assert _pinned().complete(HI).raw_text == "от Claude"
    assert capsys.readouterr().out.count("не е сума в долари") == 1


def test_the_spend_line_never_prints_a_key_put_on_the_cap_line(monkeypatch) -> None:
    """`genesis budget` не минава през маскирането на /doctor."""
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", secret)
    line = spend().summary()
    assert "не е сума в долари" in line
    assert "abcdefghij" not in line


@pytest.mark.parametrize("unknown,answer", [({"fallbacks"}, "от Claude"),
                                            ({"fallbacks", "output_config"}, "от безплатния")])
def test_an_older_sdk_does_not_break_the_turn(chain, sdk, unknown, answer) -> None:
    """SDK отпреди `fallbacks`/`output_config` вдига TypeError — тя минаваше
    покрай веригата (ловят се само RuntimeError) и ходът падаше."""
    sdk.unknown_kwargs = unknown
    assert _pinned().complete(HI).raw_text == answer


def test_odd_usage_never_breaks_a_paid_answer(sdk) -> None:
    sdk.usage = Usage(inp=float("nan"), out=10)
    text, _ = brain.Brain()._call_anthropic("k", "claude-opus-5-5", HI, None, "xhigh")
    assert text == "от Claude"


def test_the_budget_report_skips_a_damaged_cost() -> None:
    from genesis_agent import budget
    budget.record_usage(provider="anthropic", model="claude-opus-5-5", prompt_tokens=1,
                        completion_tokens=1, cost_usd=float("nan"))
    budget.record_usage(provider="anthropic", model="claude-opus-5-5", prompt_tokens=1,
                        completion_tokens=1, cost_usd=0.5)
    assert budget.today_totals()["cost_usd"] == pytest.approx(0.5)


def test_the_max_quality_chain_stops_at_the_cap_too(chain, sdk, monkeypatch) -> None:
    """`genesis fix --max` / GENESIS_QUALITY=max: Opus 5.5 и Sonnet 5.5 отпред —
    същата проверка в Brain._call, без нито една платена заявка."""
    monkeypatch.setenv("GENESIS_ANTHROPIC_MAX_USD", "1")
    spend().add(1.0, "claude-opus-5-5")
    b = brain.Brain(quality="max", use_local=False)
    assert [c["model"] for c in b.premium if c["provider"] == "anthropic"] == [
        "claude-opus-5-5", "claude-sonnet-5-5"]
    assert b.complete(HI).raw_text == "от безплатния"
    assert sdk.calls == []


# ── 6. README ────────────────────────────────────────────────────────────────

def test_the_readme_explains_the_prepaid_key() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Claude с API ключ (предплатен кредит)" in text
    section = text.split("Claude с API ключ (предплатен кредит)", 1)[1][:3000]
    for needle in ("~/.genesis/.env", "pipx inject genesis-agent anthropic",
                   "GENESIS_START_PROVIDER=anthropic", "GENESIS_START_MODEL=claude-sonnet-5-5",
                   "GENESIS_ANTHROPIC_MAX_USD=95", "/doctor", "$0.78"):
        assert needle in section, needle
