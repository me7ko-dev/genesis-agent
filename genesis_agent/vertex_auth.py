"""genesis_agent.vertex_auth — достъп до Google Vertex AI през неговия
OpenAI-съвместим endpoint.

Защо през него, а не през `google-genai`: цялата верига в brain.py говори
OpenAI формат (един `_http`, един превод на инструменти, един път за грешки).
Vertex предлага точно такъв endpoint, така че Gemini влиза като нормален
доставчик, вместо като втори native клон до `_call_anthropic`.

Двата адреса са ПРОВЕРЕНИ срещу живите услуги (2026-09-20), не преписани от
паметта — заявка с невалиден Bearer връща 401 „Expected OAuth 2 access token"
от самия Vertex, тоест пътят съществува:

    регионален : https://{LOCATION}-aiplatform.googleapis.com/v1beta1
                 /projects/{PROJECT}/locations/{LOCATION}/endpoints/openapi
    global     : https://aiplatform.googleapis.com/v1beta1
                 /projects/{PROJECT}/locations/global/endpoints/openapi

Разликата от другите доставчици: тук няма статичен ключ. Достъпът е OAuth
токен от Application Default Credentials, който живее около час — затова се
взима при извикване и се кешира до малко преди изтичането си.

Настройва се така (в ~/.genesis/.env или като променливи на средата):

    GOOGLE_CLOUD_PROJECT=моят-проект          # задължително
    GOOGLE_CLOUD_LOCATION=global              # по избор (global по подразбиране)
    GOOGLE_APPLICATION_CREDENTIALS=/път/до/service-account.json   # или `gcloud auth application-default login`
    GOOGLE_VERTEX_BASE_URL=...                # по избор: пълен override на адреса

Няколко проекта: `GOOGLE_CLOUD_PROJECT_2`, `_3`, ... Всеки проект в Google
Cloud има СВОЯ квота и се таксува отделно — това е разрешено ротиране на
собствени ресурси, за разлика от няколко безплатни акаунта при един доставчик
(виж бележката в brain.py). Кой проект колко струва решава операторът.

Без `google-auth` или без конфигуриран проект модулът просто казва „не съм
готов" и веригата подминава Vertex, както подминава доставчик без ключ.
"""
from __future__ import annotations

import os
import time

# Токенът живее ~60 минути. Подновяваме 5 минути по-рано, за да не се случи
# изтичане между проверката и самата заявка.
_REFRESH_MARGIN_SEC = 300

# project → (токен, момент на изтичане). Един процес говори с малко проекти.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def projects() -> list[str]:
    """Проектите, които операторът е настроил, по ред: основният и номерираните.

    Празен списък значи „Vertex не е конфигуриран" — не грешка.
    """
    names = ["GOOGLE_CLOUD_PROJECT"] + [f"GOOGLE_CLOUD_PROJECT_{i}" for i in range(2, 11)]
    out: list[str] = []
    for env_name in names:
        value = (os.environ.get(env_name) or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def location() -> str:
    return (os.environ.get("GOOGLE_CLOUD_LOCATION") or "global").strip() or "global"


def endpoint(project: str, loc: str | None = None) -> str:
    """Базовият адрес за този проект — без `/chat/completions`, защото
    `Brain._http` го добавя сам, както за всеки друг доставчик."""
    override = (os.environ.get("GOOGLE_VERTEX_BASE_URL") or "").strip()
    if override:
        return override.rstrip("/")
    loc = (loc or location()).strip() or "global"
    # `global` няма регионален префикс — това е отделен хост, не празен префикс.
    host = ("https://aiplatform.googleapis.com" if loc == "global"
            else f"https://{loc}-aiplatform.googleapis.com")
    return f"{host}/v1beta1/projects/{project}/locations/{loc}/endpoints/openapi"


def auth_available() -> bool:
    """Инсталиран ли е `google-auth`. Отделно от това дали има креденшъли."""
    try:
        import google.auth  # noqa: F401
    except ImportError:
        return False
    return True


def token(project: str) -> str | None:
    """Пресен OAuth токен за този проект, или None ако не може да се вземе.

    Никога не хвърля: липсващ пакет, липсващи ADC и изтекъл refresh са все
    „този доставчик не е наличен сега", а веригата има още доставчици.
    """
    cached = _TOKEN_CACHE.get(project)
    if cached and time.time() < cached[1] - _REFRESH_MARGIN_SEC:
        return cached[0]
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(scopes=[_SCOPE])
        credentials.refresh(google.auth.transport.requests.Request())
    except Exception:
        return None
    value = getattr(credentials, "token", None)
    if not value:
        return None
    expiry = getattr(credentials, "expiry", None)
    # `expiry` е naive UTC datetime в google-auth; при липса приемаме час.
    expires_at = time.time() + 3600
    if expiry is not None:
        try:
            import calendar
            expires_at = calendar.timegm(expiry.timetuple())
        except Exception:
            pass
    _TOKEN_CACHE[project] = (value, expires_at)
    return value


def ready() -> bool:
    """Има ли изобщо смисъл да се опитва Vertex."""
    return bool(projects()) and auth_available()


def status() -> str:
    """Един ред за `genesis models` / setup — какво липсва, ако липсва."""
    if not projects():
        return "Vertex: не е настроен (липсва GOOGLE_CLOUD_PROJECT)"
    if not auth_available():
        return "Vertex: липсва пакетът google-auth (pip install 'genesis-agent[google]')"
    first = projects()[0]
    if token(first) is None:
        return ("Vertex: няма валидни credentials — пусни "
                "`gcloud auth application-default login` или задай "
                "GOOGLE_APPLICATION_CREDENTIALS")
    return f"Vertex: готов ({len(projects())} проект(а), локация {location()})"
