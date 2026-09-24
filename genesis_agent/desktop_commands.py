"""genesis_agent.desktop_commands — `/` командите на терминала за Genesis Desktop.

Терминалът (genesis_terminal_agent.main) изпълнява `/status`, `/models`,
`/history`… в собствения си цикъл и ги рисува с rich. `genesis serve` ги
нямаше изобщо: текстът "/status" отиваше при модела като обикновена задача.
Тук са същите команди, но връщат ДАННИ (dict), а приложението решава как да
ги покаже. Изпълняват се без модел и без да чакат текущия ход — освен онези,
които пипат историята на разговора.

Всяка команда е `name(arg: dict, ctx: CommandContext) -> dict` и никога не
хвърля към сървъра: грешката става {"ok": False, "error": "..."}.
"""
from __future__ import annotations

import glob
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Безплатните квоти, които кодът на Genesis вече документира (коментарите в
# genesis_terminal_agent.PROVIDERS / FREE_PROVIDERS). Доставчиците не връщат
# остатъка в отговора, затова това е ориентир, а не показание на сметката.
KNOWN_QUOTAS: dict[str, dict[str, Any]] = {
    "ollama_cloud": {"tokens": 5_000_000, "days": 7, "label": "~5M токена седмично"},
    "cerebras": {"tokens": 1_000_000, "days": 1, "label": "1M токена на ден"},
}


@dataclass
class CommandContext:
    """Какво командите пипат извън модулите: живата история на `serve`."""
    get_messages: Callable[[], Any]
    set_messages: Callable[[Any], None]
    busy: Callable[[], bool]
    emit: Callable[..., Any]


def _gta():
    import genesis_terminal_agent as gta
    return gta


def _provider_name(key: str) -> str:
    name = str(_gta().PROVIDERS.get(key, {}).get("name") or key)
    # "⚡ Groq" → "Groq": иконите са за терминала, приложението има свои.
    return name.split(" ", 1)[1].strip() if " " in name and not name[0].isalnum() else name


def _estimate(messages: Any) -> int:
    gta = _gta()
    return sum(gta.estimate_tokens(str(m.get("content") or "")) for m in messages)


# ── /status ────────────────────────────────────────────────────────────────

def cmd_status(_arg: dict, ctx: CommandContext) -> dict:
    gta = _gta()
    from genesis_agent import __version__
    window = int(gta.DEFAULT_CONTEXT_WINDOW)
    used = _estimate(ctx.get_messages())
    prov = gta.current_provider
    return {
        "ok": True,
        "version": __version__,
        "provider": prov,
        "provider_name": _provider_name(prov),
        "model": gta.current_model_id,
        "free": gta.is_free_model(prov, gta.current_model_id),
        "coding_mode": bool(gta._CODING_MODE),
        "local_only": gta._LOCAL_ONLY_MODEL,
        "elapsed": gta.get_elapsed_time(),
        "session_input_tokens": int(gta.total_input_tokens),
        "session_output_tokens": int(gta.total_output_tokens),
        "context_used": used,
        "context_window": window,
        "messages": sum(1 for m in ctx.get_messages() if m.get("role") in ("user", "assistant")),
        "workspace": str(gta.WORKSPACE),
    }


# ── /usage, /cost ──────────────────────────────────────────────────────────

def _local_day(ts: str) -> date | None:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date()


def usage_report(entries: Any, *, days: int = 30, today: date | None = None,
                 is_free: Callable[[str, str], bool] | None = None) -> dict:
    """Обобщение на budget_log записите — чиста функция, за тестове.

    Дните са по местно време: „днес" за човека пред екрана, не по UTC.
    """
    today = today or datetime.now().astimezone().date()
    is_free = is_free or (lambda _p, _m: True)
    first = today - timedelta(days=days - 1)
    week_start = today - timedelta(days=6)

    def blank() -> dict:
        return {"calls": 0, "tokens": 0, "prompt": 0, "completion": 0, "cached": 0}

    periods = {"today": blank(), "week": blank(), "month": blank(), "all": blank()}
    daily: dict[str, dict] = {(first + timedelta(days=i)).isoformat(): {"calls": 0, "tokens": 0}
                              for i in range(days)}
    providers: dict[str, dict] = defaultdict(lambda: {"calls": 0, "tokens": 0, "week": 0, "today": 0})
    models: dict[tuple, dict] = defaultdict(lambda: {"calls": 0, "tokens": 0})
    paid_tokens = 0
    first_seen: date | None = None

    for e in entries:
        d = _local_day(str(e.get("ts", "")))
        if d is None:
            continue
        p, m = str(e.get("provider") or "?"), str(e.get("model") or "?")
        pt, ct = int(e.get("prompt_tokens") or 0), int(e.get("completion_tokens") or 0)
        tot = int(e.get("total_tokens") or pt + ct)
        cached = int(e.get("cached_read_tokens") or 0)
        first_seen = d if first_seen is None or d < first_seen else first_seen
        hits = ["all"]
        if d >= first:
            hits.append("month")
        if d >= week_start:
            hits.append("week")
        if d == today:
            hits.append("today")
        for h in hits:
            b = periods[h]
            b["calls"] += 1
            b["tokens"] += tot
            b["prompt"] += pt
            b["completion"] += ct
            b["cached"] += cached
        if d.isoformat() in daily:
            daily[d.isoformat()]["calls"] += 1
            daily[d.isoformat()]["tokens"] += tot
        pr = providers[p]
        pr["calls"] += 1
        pr["tokens"] += tot
        if d >= week_start:
            pr["week"] += tot
        if d == today:
            pr["today"] += tot
        mo = models[(p, m)]
        mo["calls"] += 1
        mo["tokens"] += tot
        if not is_free(p, m):
            paid_tokens += tot

    quotas = []
    for key, q in KNOWN_QUOTAS.items():
        if key not in providers:
            continue
        spent = providers[key]["week"] if q["days"] == 7 else providers[key]["today"]
        quotas.append({"provider": key, "limit": q["tokens"], "spent": spent,
                       "left": max(0, q["tokens"] - spent), "label": q["label"],
                       "period": "седмица" if q["days"] == 7 else "ден"})

    return {
        "ok": True,
        "days": days,
        "periods": periods,
        "daily": [{"day": k, **v} for k, v in daily.items()],
        "providers": sorted(({"provider": k, **v, "free": all(
            is_free(k, m) for (pp, m) in models if pp == k)} for k, v in providers.items()),
            key=lambda r: -r["tokens"]),
        "models": sorted(({"provider": p, "model": m, **v, "free": is_free(p, m)}
                          for (p, m), v in models.items()), key=lambda r: -r["tokens"])[:12],
        "paid_tokens": paid_tokens,
        "quotas": quotas,
        "since": first_seen.isoformat() if first_seen else None,
    }


def cmd_usage(arg: dict, _ctx: CommandContext) -> dict:
    from genesis_agent import budget
    gta = _gta()
    try:
        days = max(7, min(90, int(arg.get("days") or 30)))
    except (TypeError, ValueError):
        days = 30
    report = usage_report(budget._read_entries() or [], days=days, is_free=gta.is_free_model)
    for row in report["providers"] + report["quotas"]:
        row["name"] = _provider_name(row["provider"])
    return report


# ── /models, /model ────────────────────────────────────────────────────────

def cmd_models(_arg: dict, _ctx: CommandContext) -> dict:
    gta = _gta()
    chain = [{"provider": fb["provider"], "name": _provider_name(fb["provider"]), "model": fb["model"],
              "free": gta.is_free_model(fb["provider"], fb["model"]),
              "active": fb["provider"] == gta.current_provider and fb["model"] == gta.current_model_id}
             for fb in gta.FALLBACK_CHAIN]
    providers = []
    for key in gta.PROVIDERS:
        ready, hint = gta.provider_ready(key)
        providers.append({"provider": key, "name": _provider_name(key), "ready": ready,
                          "hint": "" if ready else hint, "active": key == gta.current_provider})
    return {"ok": True, "chain": chain, "providers": providers,
            "current": {"provider": gta.current_provider, "model": gta.current_model_id}}


def cmd_provider_models(arg: dict, _ctx: CommandContext) -> dict:
    gta = _gta()
    key = str(arg.get("provider") or "")
    ready, hint = gta.provider_ready(key)
    if not ready:
        return {"ok": False, "error": hint}
    models = gta.fetch_models(key) or []
    if models == ["__no_models__"]:
        return {"ok": False, "error": "Ollama работи, но няма изтеглени модели (ollama pull <модел>)."}
    if not models:
        return {"ok": False, "error": "Доставчикът не върна модели. Провери ключа и връзката."}
    return {"ok": True, "provider": key, "models": [
        {"model": m, "free": gta.is_free_model(key, m),
         "active": key == gta.current_provider and m == gta.current_model_id} for m in models]}


def cmd_set_model(arg: dict, _ctx: CommandContext) -> dict:
    gta = _gta()
    key, model = str(arg.get("provider") or ""), str(arg.get("model") or "")
    ready, hint = gta.provider_ready(key)
    if not ready:
        return {"ok": False, "error": hint}
    if not model:
        return {"ok": False, "error": "няма модел"}
    gta.current_provider, gta.current_model_id = key, model
    return {"ok": True, "provider": key, "model": model,
            "note": "Кодинг режимът е включен и ще подмине този избор." if gta._CODING_MODE else ""}


# ── режими ─────────────────────────────────────────────────────────────────

def cmd_maxcoding(_arg: dict, _ctx: CommandContext) -> dict:
    gta = _gta()
    gta._CODING_MODE = not gta._CODING_MODE
    chain: list[str] = []
    if gta._CODING_MODE:
        from genesis_agent.brain import _load_coding_chain
        chain = [f"{c['provider']}/{c['model']}" for c in _load_coding_chain()]
    return {"ok": True, "on": gta._CODING_MODE, "chain": chain}


def _local(tier: str) -> dict:
    gta = _gta()
    from genesis_agent.brain import set_local_only
    gta._LOCAL_ONLY_MODEL = None if gta._LOCAL_ONLY_MODEL == tier else tier
    set_local_only(gta._LOCAL_ONLY_MODEL)
    return {"ok": True, "on": bool(gta._LOCAL_ONLY_MODEL), "model": gta._LOCAL_ONLY_MODEL}


def cmd_local_max(_arg: dict, _ctx: CommandContext) -> dict:
    from genesis_agent.brain import LOCAL_TIER_MAX
    return _local(LOCAL_TIER_MAX)


def cmd_local_normal(_arg: dict, _ctx: CommandContext) -> dict:
    from genesis_agent.brain import LOCAL_TIER_NORMAL
    return _local(LOCAL_TIER_NORMAL)


# ── /skills, /tasks, /done, /drop ──────────────────────────────────────────

def cmd_skills(_arg: dict, _ctx: CommandContext) -> dict:
    from genesis_agent.skill_loader import format_skill_list
    return {"ok": True, "text": format_skill_list()}


def cmd_tasks(_arg: dict, _ctx: CommandContext) -> dict:
    from genesis_agent import workspace_memory as wm
    return {"ok": True, "text": wm.briefing(max_threads=20, max_decisions=10), "stats": wm.stats()}


def _close(arg: dict, drop: bool) -> dict:
    from genesis_agent import workspace_memory as wm
    ids = [i for i in str(arg.get("ids") or "").replace(",", " ").split() if i]
    if not ids:
        return {"ok": False, "error": "Дай номер: /done 3 (готово) или /drop 3 (изхвърли)"}
    return {"ok": True, "text": "\n".join(wm.close_thread(i, drop=drop) for i in ids)}


def cmd_done(arg: dict, _ctx: CommandContext) -> dict:
    return _close(arg, drop=False)


def cmd_drop(arg: dict, _ctx: CommandContext) -> dict:
    return _close(arg, drop=True)


# ── /history ───────────────────────────────────────────────────────────────

def _history_files() -> list[str]:
    return sorted(glob.glob(str(_gta().HISTORY_DIR / "session_*.json")), reverse=True)


def _preview(messages: list) -> str:
    for m in messages:
        if m.get("role") == "user":
            text = " ".join(str(m.get("content") or "").split())
            return text[:140]
    return ""


def cmd_history(_arg: dict, _ctx: CommandContext) -> dict:
    items = []
    for path in _history_files()[:30]:
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            continue
        convo = [m for m in loaded if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
        if not convo:
            continue
        items.append({"file": Path(path).name, "modified": int(os.path.getmtime(path) * 1000),
                      "messages": len(convo), "preview": _preview(convo)})
    return {"ok": True, "sessions": items}


def cmd_load_history(arg: dict, ctx: CommandContext) -> dict:
    gta = _gta()
    if ctx.busy():
        return {"ok": False, "error": "busy"}
    name = Path(str(arg.get("file") or "")).name  # само име: никакви пътища отвън
    path = gta.HISTORY_DIR / name
    if not name.startswith("session_") or not path.is_file():
        return {"ok": False, "error": "няма такава сесия"}
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    system = ctx.get_messages()[0]["content"]
    messages = gta._restore_session(loaded, system)
    ctx.set_messages(messages)
    # Следващите ходове се пишат в нов файл: старата сесия остава непокътната.
    gta.session_start_time = time.time()
    ctx.emit("cleared")
    for m in messages:
        if m.get("role") == "user":
            ctx.emit("user", text=str(m.get("content") or ""))
        elif m.get("role") == "assistant" and m.get("content"):
            ctx.emit("assistant", text=str(m.get("content")))
    return {"ok": True, "messages": sum(1 for m in messages if m.get("role") in ("user", "assistant"))}


# ── /backup, /update ───────────────────────────────────────────────────────

def cmd_backup(_arg: dict, _ctx: CommandContext) -> dict:
    gta = _gta()
    dest = os.environ.get("GENESIS_BACKUP_DIR", "").strip()
    if not dest:
        return {"ok": False, "error": "Задай GENESIS_BACKUP_DIR (къде да пази архива), напр.: "
                                      'setx GENESIS_BACKUP_DIR "D:\\backup\\genesis" и рестартирай агента.'}
    ok, err = gta._backup_workspace(gta.WORKSPACE, Path(dest).expanduser())
    return {"ok": ok, "error": err[:300] if not ok else "", "dest": dest, "source": str(gta.WORKSPACE)}


def cmd_update(_arg: dict, _ctx: CommandContext) -> dict:
    from genesis_agent import __version__, version_info
    check = version_info.check_update()
    src = check.src
    out: dict[str, Any] = {"ok": True, "version": __version__, "up_to_date": check.up_to_date,
                           "current": src.short if src else None, "ref": src.ref if src else None,
                           "latest": check.latest[:7] if check.latest else None, "changes": [],
                           "command": version_info.install_command(src) if src else None}
    if src and check.latest and not check.up_to_date:
        out["changes"] = version_info.changelog(src.owner_repo, src.commit, check.latest)
    return out


COMMANDS: dict[str, Callable[[dict, CommandContext], dict]] = {
    "status": cmd_status,
    "usage": cmd_usage,
    "models": cmd_models,
    "provider_models": cmd_provider_models,
    "set_model": cmd_set_model,
    "maxcoding": cmd_maxcoding,
    "local_max": cmd_local_max,
    "local_normal": cmd_local_normal,
    "skills": cmd_skills,
    "tasks": cmd_tasks,
    "done": cmd_done,
    "drop": cmd_drop,
    "history": cmd_history,
    "load_history": cmd_load_history,
    "backup": cmd_backup,
    "update": cmd_update,
}


def run(name: str, arg: dict, ctx: CommandContext) -> dict:
    fn = COMMANDS.get(name)
    if fn is None:
        return {"ok": False, "error": f"непозната команда: {name}"}
    try:
        return fn(arg if isinstance(arg, dict) else {}, ctx)
    except Exception as e:  # командите никога не събарят сървъра
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
