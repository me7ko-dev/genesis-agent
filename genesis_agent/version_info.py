"""genesis_agent.version_info — коя версия работи и има ли по-нова.

Защо съществува: `genesis --version` печаташе `0.1.0` и преди, и след
обновяване, защото номерът се вдига рядко, а кодът се мени всеки ден. Тоест
на въпроса „това новата версия ли е" нямаше отговор — нито за оператора,
нито за мен. Оттам и разумното желание „я да изтрия всичко и да инсталирам
наново", което изтрива и ключовете, и паметта, за да реши въпрос, който се
решава с един ред.

pip вече знае отговора и го записва: инсталация от git оставя
`direct_url.json` (PEP 610) с ТОЧНИЯ комит и поискания клон:

    {"url": "https://github.com/…/genesis-agent",
     "vcs_info": {"commit_id": "dc7f65a…", "requested_revision": "claude/…"}}

Проверено срещу реална инсталация от клона на 2026-09-20, не по памет.

Мрежата се пипа само при изрично `genesis update` — стартирането не пита
никого нищо.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

_DIST = "genesis-agent"
_API = "https://api.github.com/repos/{owner_repo}/commits/{ref}"
_TIMEOUT = 15


@dataclass
class Source:
    """Откъде е дошло инсталираното копие."""
    url: str = ""
    commit: str = ""
    ref: str = ""

    @property
    def short(self) -> str:
        return self.commit[:7]

    @property
    def owner_repo(self) -> str:
        """`me7ko-dev/genesis-agent` от адреса, или празно, ако не е GitHub."""
        text = self.url.split("github.com", 1)[-1] if "github.com" in self.url else ""
        text = text.lstrip(":/").removesuffix(".git").strip("/")
        parts = text.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else ""


def installed_source() -> Source | None:
    """Комитът и клонът на инсталираното копие, или None.

    None значи „не е инсталирано от git" — чекаут за разработка, инсталация
    от архив, копирана папка. Това не е грешка и не бива да се съобщава като
    такава.
    """
    try:
        from importlib.metadata import distribution
        raw = distribution(_DIST).read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    vcs = data.get("vcs_info") or {}
    commit = vcs.get("commit_id") or ""
    if not commit:
        return None
    return Source(url=data.get("url") or "", commit=commit,
                  ref=vcs.get("requested_revision") or "")


def describe(version: str) -> str:
    """Редът за `genesis --version`."""
    src = installed_source()
    if src is None:
        return f"genesis-agent {version}"
    ref = f", {src.ref}" if src.ref else ""
    return f"genesis-agent {version} ({src.short}{ref})"


def latest_commit(owner_repo: str, ref: str, *, timeout: int = _TIMEOUT) -> str | None:
    """Върхът на този клон в GitHub, или None ако не може да се вземе.

    Никога не хвърля: липсваща мрежа, променен API и лимит на заявките са
    все „не знам сега", а не повод командата да се счупи.
    """
    if not owner_repo or not ref:
        return None
    request = urllib.request.Request(
        _API.format(owner_repo=owner_repo, ref=ref),
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "genesis-agent"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(1 << 20).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) and sha else None


def install_command(src: Source) -> str:
    """Командата, която обновява точно това копие."""
    ref = f"@{src.ref}" if src.ref else ""
    return f'pipx install --force "git+{src.url}{ref}"'


def update_report(*, version: str, timeout: int = _TIMEOUT) -> str:
    """Човешкият отговор на „това новата версия ли е"."""
    src = installed_source()
    if src is None:
        return ("Това копие не е инсталирано от git (чекаут за разработка или "
                "разархивирано), затова няма с какво да се сравни.\n"
                "В чекаут: `git pull`.")
    head = f"genesis-agent {version} — {src.short}" + (f" ({src.ref})" if src.ref else "")
    latest = latest_commit(src.owner_repo, src.ref, timeout=timeout)
    if latest is None:
        return (f"{head}\nНе можах да питам GitHub (мрежа или лимит). "
                f"Обновяване:\n  {install_command(src)}")
    if latest == src.commit:
        return f"{head}\n✅ Това е най-новото на {src.ref or 'този клон'}."
    return (f"{head}\n⬆️  Има по-ново: {latest[:7]}. Обнови с:\n"
            f"  {install_command(src)}\n"
            "После отвори НОВ терминал и пусни `genesis --version` — "
            "комитът трябва да е новият.")
