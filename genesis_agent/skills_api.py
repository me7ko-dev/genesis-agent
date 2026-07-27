#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
genesis_agent.skills_api — преизползване на съществуващи умения като градивни блокове.

Позволява на генерирания код (и на теб) да зарежда verified умение и да ползва
неговите функции/класове, вместо да пише всичко от нула:

    from genesis_agent.skills_api import load
    mod = load("dedupe_lines_preserve_order")
    result = mod.dedupe_lines(["a", "b", "a"])

Или директно функция:

    from genesis_agent.skills_api import get_function
    dedupe = get_function("dedupe_lines_preserve_order", "dedupe_lines")

Кодът на умението се изпълнява в отделно module namespace (не в глобалния),
и по подразбиране само verified умения се зареждат (безопасност).
"""
from __future__ import annotations

import types
from typing import Any, Callable

from genesis_agent.skill_loader import skill_view, load_skills_index


def list_available(verified_only: bool = True) -> list[str]:
    """Имена на наличните умения (по подразбиране само verified)."""
    idx = load_skills_index()
    out = []
    for name, meta in idx.items():
        if verified_only and not meta.get("verified"):
            continue
        out.append(name)
    return sorted(out)


def load(name: str, *, verified_only: bool = True) -> types.ModuleType:
    """
    Зарежда умение като модул (изпълнява кода в изолиран namespace) и връща го.
    Достъпваш функциите му като атрибути: load('x').my_func(...).
    """
    idx = load_skills_index()
    meta = idx.get(name)
    if verified_only and meta and not meta.get("verified"):
        raise PermissionError(
            f"Умението '{name}' не е verified. Ползвай verified_only=False на своя отговорност."
        )
    data = skill_view(name)
    code = data["code"]
    mod = types.ModuleType(f"genesis_skill_{name}")
    mod.__dict__["__name__"] = f"genesis_skill_{name}"
    # Изпълняваме БЕЗ __main__ секцията (за да не пуска self-test-а при import).
    # Умението се дефинира; self-test под if __name__=="__main__" не се задейства.
    exec(compile(code, f"<skill:{name}>", "exec"), mod.__dict__)
    return mod


def get_function(skill_name: str, func_name: str, *, verified_only: bool = True) -> Callable[..., Any]:
    """Връща конкретна функция от умение."""
    mod = load(skill_name, verified_only=verified_only)
    fn = getattr(mod, func_name, None)
    if not callable(fn):
        raise AttributeError(f"Умението '{skill_name}' няма функция '{func_name}'.")
    return fn


if __name__ == "__main__":
    print("Налични verified умения:", len(list_available()))
    # Демонстрация: зареди умение и преизползвай функцията му (композиция).
    try:
        mod = load("flatten_nested_dict", verified_only=False)
        result = mod.flatten({"user": {"name": "потребителят", "prefs": {"lang": "bg"}}})
        print("flatten_nested_dict преизползвано →", result)
    except Exception as e:
        print("демо пропуснато:", e)
