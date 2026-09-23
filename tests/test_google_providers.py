"""Google като доставчик в веригата — Gemini (AI Studio) и Vertex AI.

И двата адреса са проверени срещу живите услуги на 2026-09-20 — заявка с
невалиден ключ връща 400 „Please pass a valid API key" от generativelanguage
и 401 „Expected OAuth 2 access token" от Vertex. Тези тестове заковават точно
тези адреси: сгрешен път не гърми при стартиране, а дава 404 посред мисия.

Разликата между двата е причината Vertex да има свой клон: Gemini е обикновен
доставчик със статичен ключ, а Vertex няма ключ изобщо — адресът зависи от
проекта и локацията, а достъпът е OAuth токен с живот около час.
"""
from __future__ import annotations

import pytest

from genesis_agent import brain as br
from genesis_agent import vertex_auth as va
from genesis_agent.brain import _PROVIDERS, Brain


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_VERTEX_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for i in range(2, 11):
        monkeypatch.delenv(f"GOOGLE_CLOUD_PROJECT_{i}", raising=False)
    va._TOKEN_CACHE.clear()
    br._EXHAUSTED.clear()
    yield
    va._TOKEN_CACHE.clear()
    br._EXHAUSTED.clear()


def _capture(monkeypatch) -> list[dict]:
    """Прихваща заявките на ниво `_http`, без мрежа."""
    calls: list[dict] = []

    def _fake_http(self, base_url, key, model, messages, timeout, tools=None, extra=None):
        calls.append({"base_url": base_url, "key": key, "model": model})
        return ("готово", None)

    monkeypatch.setattr(Brain, "_http", _fake_http)
    return calls


class TestGeminiIsAnOrdinaryProvider:
    def test_the_verified_base_url_is_what_ships(self) -> None:
        base_url, key_env = _PROVIDERS["gemini"]
        assert base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
        assert key_env == "GEMINI_API_KEY"

    def test_the_base_url_stops_before_chat_completions(self) -> None:
        """`_http` добавя `/chat/completions` сам — адрес, който вече го
        съдържа, дава двоен път."""
        assert not _PROVIDERS["gemini"][0].endswith("/chat/completions")

    def test_a_call_goes_out_with_the_key_as_a_bearer(self, monkeypatch) -> None:
        calls = _capture(monkeypatch)
        brain = Brain()
        brain.keys = {"GEMINI_API_KEY": "ключ-123"}
        brain._call("gemini", "gemini-2.5-flash", [{"role": "user", "content": "здр"}])
        assert calls[0]["key"] == "ключ-123"
        assert calls[0]["base_url"].startswith("https://generativelanguage.googleapis.com")

    def test_without_a_key_it_is_skipped_not_failed(self, monkeypatch) -> None:
        _capture(monkeypatch)
        brain = Brain()
        brain.keys = {}
        with pytest.raises(RuntimeError, match="skip:"):
            brain._call("gemini", "gemini-2.5-flash", [{"role": "user", "content": "x"}])


class TestVertexResolvesItsOwnAddressAndToken:
    def test_no_project_is_a_skip_not_a_crash(self, monkeypatch) -> None:
        _capture(monkeypatch)
        with pytest.raises(RuntimeError, match="skip:"):
            Brain()._call("vertex", "google/gemini-2.5-pro", [{"role": "user", "content": "x"}])

    def test_it_calls_the_verified_endpoint_with_the_oauth_token(
        self, monkeypatch
    ) -> None:
        calls = _capture(monkeypatch)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "моят-проект")
        monkeypatch.setattr(va, "auth_available", lambda: True)
        monkeypatch.setattr(va, "token", lambda _p: "ya29.токен")

        Brain()._call("vertex", "google/gemini-2.5-pro", [{"role": "user", "content": "x"}])
        assert calls[0]["key"] == "ya29.токен"
        assert calls[0]["base_url"] == (
            "https://aiplatform.googleapis.com/v1beta1/projects/моят-проект"
            "/locations/global/endpoints/openapi")

    def test_missing_google_auth_is_a_skip(self, monkeypatch) -> None:
        _capture(monkeypatch)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "демо")
        monkeypatch.setattr(va, "auth_available", lambda: False)
        with pytest.raises(RuntimeError, match="skip:"):
            Brain()._call("vertex", "google/gemini-2.5-pro", [{"role": "user", "content": "x"}])

    def test_credentials_that_cannot_be_refreshed_are_a_skip(self, monkeypatch) -> None:
        _capture(monkeypatch)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "демо")
        monkeypatch.setattr(va, "auth_available", lambda: True)
        monkeypatch.setattr(va, "token", lambda _p: None)
        with pytest.raises(RuntimeError, match="skip:"):
            Brain()._call("vertex", "google/gemini-2.5-pro", [{"role": "user", "content": "x"}])


class TestProjectRotation:
    """Всеки проект в Google Cloud има СВОЯ квота и своя сметка. Това е
    ротация на собствени ресурси на оператора — различно от няколко безплатни
    акаунта при един доставчик, което SECURITY.md нарочно не поддържа."""

    def _two_projects(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "първи")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_2", "втори")
        monkeypatch.setattr(va, "auth_available", lambda: True)
        monkeypatch.setattr(va, "token", lambda p: f"токен-{p}")

    def test_a_rate_limited_project_steps_aside_for_the_next(self, monkeypatch) -> None:
        self._two_projects(monkeypatch)
        seen: list[str] = []

        def _fake_http(self, base_url, key, model, messages, timeout, tools=None, extra=None):
            seen.append(base_url)
            if "първи" in base_url:
                raise RuntimeError("HTTP_429: quota")
            return ("от втория", None)

        monkeypatch.setattr(Brain, "_http", _fake_http)
        text, _ = Brain()._call("vertex", "google/gemini-2.5-pro",
                                 [{"role": "user", "content": "x"}])
        assert text == "от втория"
        assert len(seen) == 2, seen

    def test_only_the_failing_project_is_cooled_down(self, monkeypatch) -> None:
        self._two_projects(monkeypatch)

        def _fake_http(self, base_url, key, model, messages, timeout, tools=None, extra=None):
            if "първи" in base_url:
                raise RuntimeError("HTTP_429: quota")
            return ("ок", None)

        monkeypatch.setattr(Brain, "_http", _fake_http)
        Brain()._call("vertex", "google/gemini-2.5-pro", [{"role": "user", "content": "x"}])
        assert br._is_exhausted("key::VERTEX#1") is True
        assert br._is_exhausted("key::VERTEX#2") is False

    def test_all_projects_cooling_down_is_a_skip_not_a_crash(self, monkeypatch) -> None:
        self._two_projects(monkeypatch)
        br._mark_exhausted("key::VERTEX#1")
        br._mark_exhausted("key::VERTEX#2")
        _capture(monkeypatch)
        with pytest.raises(RuntimeError, match="skip:"):
            Brain()._call("vertex", "google/gemini-2.5-pro", [{"role": "user", "content": "x"}])

    def test_a_non_quota_error_is_raised_not_rotated_past(self, monkeypatch) -> None:
        """400 значи „заявката е грешна" — същата заявка към втория проект ще
        се счупи точно така. Ротацията е за изчерпана квота, не за бъгове."""
        self._two_projects(monkeypatch)

        def _fake_http(self, base_url, key, model, messages, timeout, tools=None, extra=None):
            raise RuntimeError("HTTP_400: bad request")

        monkeypatch.setattr(Brain, "_http", _fake_http)
        with pytest.raises(RuntimeError, match="HTTP_400"):
            Brain()._call("vertex", "google/gemini-2.5-pro", [{"role": "user", "content": "x"}])
