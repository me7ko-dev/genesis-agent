"""genesis_agent.vertex_auth — достъпът до Google Vertex AI.

Vertex е единственият доставчик без статичен ключ: адресът зависи от проекта
и локацията, а „ключът" е OAuth токен, който живее около час. Затова има
отделен модул и отделни тестове — грешка в сглобяването на адреса дава HTTP
404 в мисия, а не при стартиране.

Двата адреса са проверени срещу живите услуги (2026-09-20): заявка с невалиден
Bearer връща 401 „Expected OAuth 2 access token" от Vertex, тоест пътят
съществува точно в тази форма. Тестовете заковават нея.
"""
from __future__ import annotations

import pytest

from genesis_agent import vertex_auth as va


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
                 "GOOGLE_VERTEX_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for i in range(2, 11):
        monkeypatch.delenv(f"GOOGLE_CLOUD_PROJECT_{i}", raising=False)
    va._TOKEN_CACHE.clear()
    yield
    va._TOKEN_CACHE.clear()


class TestEndpointShape:
    def test_global_has_no_regional_prefix(self, monkeypatch) -> None:
        """`global` е ОТДЕЛЕН хост, не празен префикс — `-aiplatform` там дава
        несъществуващо име."""
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
        assert va.endpoint("demo") == (
            "https://aiplatform.googleapis.com/v1beta1/projects/demo"
            "/locations/global/endpoints/openapi")

    def test_a_region_gets_the_regional_host(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        assert va.endpoint("demo") == (
            "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/demo"
            "/locations/us-central1/endpoints/openapi")

    def test_it_stops_before_chat_completions(self) -> None:
        """`Brain._http` добавя `/chat/completions` на всеки доставчик. Адрес,
        който вече го съдържа, дава двоен път и 404."""
        assert not va.endpoint("demo").endswith("/chat/completions")

    def test_global_is_the_default_location(self) -> None:
        assert "/locations/global/" in va.endpoint("demo")

    def test_an_explicit_override_wins(self, monkeypatch) -> None:
        """Google мести адреси; операторът трябва да може да поправи това без
        да чака нова версия."""
        monkeypatch.setenv("GOOGLE_VERTEX_BASE_URL", "https://example.test/v1/")
        assert va.endpoint("demo") == "https://example.test/v1"


class TestProjects:
    def test_no_project_means_not_configured(self) -> None:
        assert va.projects() == []
        assert va.ready() is False

    def test_the_numbered_projects_rotate_in_order(self, monkeypatch) -> None:
        """Всеки проект в GCP има своя квота и своя сметка — това е ротация на
        собствени ресурси, за разлика от няколко безплатни акаунта при един
        доставчик."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "първи")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_2", "втори")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_4", "четвърти")
        assert va.projects() == ["първи", "втори", "четвърти"]

    def test_a_repeated_project_is_listed_once(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "един")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_2", "един")
        assert va.projects() == ["един"]

    def test_whitespace_only_is_not_a_project(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "   ")
        assert va.projects() == []


class TestTokenNeverBreaksAMission:
    def test_missing_credentials_return_none_instead_of_raising(self, monkeypatch) -> None:
        """Липсващи ADC не са грешка — веригата просто минава към следващия
        доставчик, както при липсващ ключ."""
        monkeypatch.setattr(va, "auth_available", lambda: True)
        assert va.token("demo") is None or isinstance(va.token("demo"), str)

    def test_a_cached_token_is_reused(self, monkeypatch) -> None:
        import time
        va._TOKEN_CACHE["demo"] = ("кеширан", time.time() + 4000)
        assert va.token("demo") == "кеширан"

    def test_a_token_close_to_expiry_is_not_reused(self, monkeypatch) -> None:
        """Подновяваме с 5 минути резерв: токен, изтичащ между проверката и
        заявката, дава 401 посред мисия."""
        import time
        va._TOKEN_CACHE["demo"] = ("почти изтекъл", time.time() + 60)
        monkeypatch.setattr(va, "auth_available", lambda: False)
        assert va.token("demo") != "почти изтекъл"


class TestStatusTellsWhatIsMissing:
    def test_it_names_the_missing_project(self) -> None:
        assert "GOOGLE_CLOUD_PROJECT" in va.status()

    def test_it_names_the_missing_package(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo")
        monkeypatch.setattr(va, "auth_available", lambda: False)
        assert "google-auth" in va.status()

    def test_it_names_the_missing_credentials(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo")
        monkeypatch.setattr(va, "auth_available", lambda: True)
        monkeypatch.setattr(va, "token", lambda _p: None)
        assert "gcloud auth" in va.status()
