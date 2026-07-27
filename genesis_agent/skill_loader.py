#!/usr/bin/env python3
"""
Skill loader — reads the Markdown skill format used by this project.

Предоставя:
 - skill_view(name)         → метаданни + код от .md файл
 - run_skill(name, **kwargs) → изпълнява кода в изолиран subprocess
 - search_skills(query)     → търси умения по trigger keywords
 - reload_skills_index()    → hot-reload при нови умения
 - resolve_skill(query)     → точно име или fuzzy match по свободен текст
 - use_skill(query, driver) → РЕАЛНО изпълнение (умение + driver код) в sandbox
"""

from __future__ import annotations

import ast
import re
import json
import yaml
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from genesis_agent.config import PROJECT_ROOT

SKILLS_DIR = PROJECT_ROOT / "skills"
_SKILLS_INDEX_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def reload_skills_index() -> Dict[str, Dict[str, Any]]:
    """Принудително презарежда skills.json индекса от диска."""
    global _SKILLS_INDEX_CACHE
    index_path = SKILLS_DIR / "skills.json"
    if not index_path.exists():
        _SKILLS_INDEX_CACHE = {}
        return _SKILLS_INDEX_CACHE
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        skills_list = data if isinstance(data, list) else data.get("skills", [])
        _SKILLS_INDEX_CACHE = {s["name"]: s for s in skills_list if "name" in s}
    except Exception:
        _SKILLS_INDEX_CACHE = {}
    return _SKILLS_INDEX_CACHE


def load_skills_index() -> Dict[str, Dict[str, Any]]:
    """Зарежда (с кеш) skills.json индекса."""
    if _SKILLS_INDEX_CACHE is None:
        reload_skills_index()
    return _SKILLS_INDEX_CACHE  # type: ignore[return-value]


def skill_view(name: str, *, file_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Зарежда .md файл на умение и връща YAML метаданни + код.

    Raises:
        FileNotFoundError: ако файлът не съществува.
        ValueError:        ако няма Python код блок в .md.
    """
    index = load_skills_index()
    skill_meta = index.get(name)

    if not skill_meta and not file_path:
        # Опит по точно имe на файл
        guessed = SKILLS_DIR / f"{name}.md"
        if guessed.exists():
            file_path = guessed
        else:
            raise FileNotFoundError(f"Умение '{name}' не е намерено в skills.json.")

    if file_path:
        md_path = Path(file_path)
    elif skill_meta:
        md_path = PROJECT_ROOT / skill_meta["file_path"]
    else:
        raise FileNotFoundError(f"Умение '{name}' няма нито файл, нито запис в индекса.")
    if not md_path.exists():
        raise FileNotFoundError(f"Файлът на умението не съществува: {md_path}")

    content = md_path.read_text(encoding="utf-8", errors="replace")

    # Извличане на YAML frontmatter
    fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    metadata: Dict[str, Any] = {}
    if fm_match:
        try:
            metadata = yaml.safe_load(fm_match.group(1)) or {}
        except yaml.YAMLError:
            metadata = {}
    metadata.setdefault("name", name)

    # Извличане на Python код
    code_match = re.search(r"```python\n(.*?)\n```", content, re.DOTALL)
    if not code_match:
        raise ValueError(f"Няма Python код блок в: {md_path}")

    return {
        "metadata": metadata,
        "code": code_match.group(1).strip(),
        "file_path": str(md_path.relative_to(PROJECT_ROOT)),
    }


# Кратки/общи думи не носят сигнал за релевантност (напр. "a" в "reverse a string"
# съвпада с половината библиотека) — без филтър keyword search връща боклук.
_STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "for", "with", "and", "or", "is",
    "that", "this", "using", "use", "from", "by", "as", "at", "be", "it", "its",
    # goal_engine-ните шаблони обличат ПОЧТИ ВСЯКА цел в едно и също изречение
    # ("...with type hints, docstring, and an assert-based self-test that
    # prints OK", "stdlib only", "from scratch", "production-grade utility в
    # pure Python") — тези думи съвпадат между напълно несвързани цели и
    # надуваха score-а изкуствено (измерено 2026-07-27: "изчисли повърхнина на
    # тор" излизаше score=5 съвпадение с "exponential backoff retry" само
    # заради "test/assert/self/type/hints"). Без този филтър прагът по-долу
    # в build_context е безполезен — боклукът минаваше прага само с шаблонни думи.
    "implement", "build", "create", "develop", "write", "design",
    "type", "hints", "docstring", "assert", "self", "test", "tests", "prints",
    "ok", "stdlib", "only", "pure", "python", "production", "grade", "utility",
    "inline", "scratch", "based", "given",
}


def _keywords(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9_]+", text.lower())
        if len(w) >= 3 and w not in _STOPWORDS
    }


def search_skills(query: str, top_n: int = 5, *, use_semantic: bool = True) -> List[Dict[str, Any]]:
    """
    Търси умения по keyword overlap (бързо, точно за буквални съвпадения), после
    допълва с семантично търсене (genesis_agent.embeddings) за перифразирани заявки,
    които keyword match пропуска — напр. "invert character order" ~ "reverse a string".
    Ако embeddings не са налични, поведението е същото както преди (чист keyword).
    """
    index = load_skills_index()
    query_words = _keywords(query)
    scored: List[tuple[int, Dict[str, Any]]] = []

    for skill in index.values():
        triggers = skill.get("triggers", [])
        if isinstance(triggers, str):
            triggers = [triggers]
        trigger_words = set(w for t in triggers for w in _keywords(t))
        # Добавяме думи от name и description
        name_words = _keywords(skill.get("name", ""))
        desc_words = _keywords(skill.get("description", ""))
        all_words = trigger_words | name_words | desc_words
        score = len(query_words & all_words)
        if score > 0:
            # _kw_score е ПРЕХОДЕН ключ (не се пази в skills.json) — брой РЕАЛНИ
            # (пост-stopword) съвпадащи думи. build_context() го ползва, за да
            # не инжектира пълен код при съвпадение само по случайност/шаблон.
            scored.append((score, {**skill, "_kw_score": score}))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [s for _, s in scored]
    seen = {s["name"] for s in results if "name" in s}

    if use_semantic and len(results) < top_n:
        try:
            from genesis_agent.embeddings import semantic_search
            for name, sim in semantic_search(query, top_k=top_n * 3):
                if sim < 0.55 or name in seen or name not in index:
                    continue
                # Дошло е през embedding прага (≥0.55 cosine), не през keyword
                # overlap — маркираме отделно, _kw_score=0 тук НЕ значи "слабо".
                results.append({**index[name], "_semantic_hit": True})
                seen.add(name)
        except Exception:
            pass  # embeddings недостъпни — просто чист keyword резултат

    return results[:top_n]


def _extract_signatures(code: str) -> List[str]:
    """Топ-ниво def/class сигнатури от кода на умение — какво реално може да
    се извика, показано на модела ПРЕДИ да пише driver код по памет/предположение."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    sigs: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            sigs.append(f"{prefix} {node.name}({', '.join(args)})")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and not n.name.startswith("_")]
            sigs.append(f"class {node.name}({', '.join(methods)})")
    return sigs


def resolve_skill(name_or_query: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Резолвира умение по ТОЧНО име (skills.json ключ), а ако липсва — по
    свободен текст (fuzzy/семантично търсене през search_skills).
    Връща (resolved_name или None, списък кандидати от search — празен ако е било точно име).
    """
    idx = load_skills_index()
    q = name_or_query.strip()
    if q in idx:
        return q, []
    candidates = search_skills(q, top_n=3)
    if candidates:
        return candidates[0].get("name"), candidates
    return None, []


def use_skill(name_or_query: str, driver_code: str = "") -> str:
    """
    Намира умение (по точно име или свободна заявка) и го изпълнява РЕАЛНО —
    не само го инжектира като текст в промпта. driver_code (ако е зададен) се
    добавя СЛЕД кода на умението в един и същ файл, затова функциите/класовете
    на умението са директно достъпни по име, без import — резултатът е истинско
    изпълнено действие, не поредна регенерация на същата логика от нула.

    Ако driver_code е празен, само зарежда умението и показва наличните
    функции/класове (self-test-ът на умението пак се пуска — потвърждава, че
    работи).
    """
    from genesis_agent import sandbox

    resolved, candidates = resolve_skill(name_or_query)
    if not resolved:
        return f"[USE_SKILL: {name_or_query.strip()}] Няма намерено умение по това име/заявка."

    try:
        data = skill_view(resolved)
    except (FileNotFoundError, ValueError) as e:
        return f"[USE_SKILL: {resolved}] Грешка при зареждане: {e}"

    sigs = _extract_signatures(data["code"])
    header = [f"[USE_SKILL: {resolved}]"]
    if resolved != name_or_query.strip():
        header.append(f"(намерено по заявка '{name_or_query.strip()}')")
    if sigs:
        header.append("Достъпни: " + "; ".join(sigs))
    others = [c["name"] for c in candidates if c.get("name") and c["name"] != resolved]
    if others:
        header.append("Други близки умения: " + ", ".join(others))

    script = data["code"]
    if driver_code.strip():
        script += "\n\n# --- USE_SKILL driver ---\n" + driver_code

    res = sandbox.run_python(script, timeout=60)
    if res.blocked:
        return "\n".join(header) + "\n" + res.stderr

    body = (res.stdout or "").strip()
    if res.stderr and not res.ok:
        body += ("\n" if body else "") + "stderr:\n" + res.stderr.strip()[:2000]
        body += ("\n\nГРЕШКА — driver кодът не се изпълни. Провери 'Достъпни' по-горе "
                 "за точното име на функцията/класа и опитай ОТНОВО с коригиран driver "
                 "в нов [USE_SKILL: ...] таг. НЕ съобщавай резултат, който не е реално "
                 "отпечатан по-горе.")
    if not body:
        body = "(няма изход)" if res.ok else f"грешка (rc={res.returncode})"
    return "\n".join(header) + "\n" + body


def run_skill(name: str, **exec_kwargs) -> str:
    """
    Изпълнява умение през genesis_agent.sandbox (защитната бариера).
    exec_kwargs се подават като env var SKILL_ARGS (JSON) — през whitelist-a,
    затова кодът на умението не вижда API ключовете на процеса-родител.
    Връща stdout на процеса.
    """
    import json as _json
    from genesis_agent import sandbox

    data = skill_view(name)
    code = data["code"]

    res = sandbox.run_python(
        code,
        timeout=120,
        env_extra={"SKILL_ARGS": _json.dumps(exec_kwargs)},
    )
    if res.blocked:
        raise RuntimeError(res.stderr)
    if not res.ok:
        raise RuntimeError(f"Грешка при изпълнение:\n{res.stderr[:2000]}")
    return res.stdout.strip()


if __name__ == "__main__":
    from pprint import pprint
    idx = load_skills_index()
    print(f"✅ Заредени умения: {len(idx)}")

    results = search_skills("retry decorator")
    print(f"\n🔎 Търсене 'retry decorator' → {len(results)} резултата:")
    for r in results:
        print(f"  - {r['name']} ({r.get('category', '?')})")