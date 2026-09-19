#!/usr/bin/env python3
"""
genesis_agent/notifier.py — Multi-channel delivery за Genesis Agent.

Поддържа: Telegram.
Конфигурира се от .env:
    GENESIS_TELEGRAM_TOKEN, GENESIS_TELEGRAM_CHAT_ID

Употреба:
    from genesis_agent.notifier import send_message
    send_message("✅ Задача изпълнена: retry-decorator")
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from genesis_agent.paths import CONFIG_PATH as _CONFIG_YAML

# ─── Разрешаване на конфигурация (env → .env файлове → config.yaml) ────────────
# Ядрото (autonomous loop и т.н.) не зарежда .env в os.environ, затова notifier-ът
# сам намира webhook-а от същите източници, които ползва терминалният агент.


def _from_env_files(key: str) -> str:
    """Стойността на ключ от .env файловете, или "" ако липсва.

    Делегира на paths.read_env_files вместо да преповтаря четенето (bug fix,
    2026-08-12). Локалното копие тук имаше два дефекта, които общата функция
    отдавна няма:

      • съпоставяше имената с `line.startswith(key)` — ПРЕФИКС, не точно
        съвпадение, така че `GENESIS_TELEGRAM_TOKEN` хващаше и
        `GENESIS_TELEGRAM_TOKEN_2=...`, ако то стои по-нагоре във файла.
        Тук цената е известията да заминат към ЧУЖД бот — не просто грешна
        конфигурация, а изпращане на съдържание не където трябва.
      • не махаше inline коментар, тоест
        `GENESIS_TELEGRAM_TOKEN=123:abc # моят бот` връщаше стойност със
        залепен коментар и заявката просто се проваляше.
    """
    from genesis_agent.paths import read_env_files
    return read_env_files(key) or ""


def _from_config_yaml(section: str, field: str) -> str:
    if not _CONFIG_YAML.exists():
        return ""
    try:
        import yaml
        data = yaml.safe_load(_CONFIG_YAML.read_text(encoding="utf-8")) or {}
        return (data.get(section, {}) or {}).get(field, "") or ""
    except Exception:
        return ""


def resolve_setting(env_key: str, *, yaml_section: str = "", yaml_field: str = "") -> str:
    """Намира настройка от os.environ, после .env файловете, после config.yaml."""
    val = os.environ.get(env_key, "")
    if not val:
        val = _from_env_files(env_key)
    if not val and yaml_section and yaml_field:
        val = _from_config_yaml(yaml_section, yaml_field)
    return val


# ─── Telegram ────────────────────────────────────────────────────────────────

def _send_telegram(text: str, token: str, chat_id: str) -> bool:
    """Изпраща съобщение чрез Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.URLError as e:
        print(f"[notifier] Telegram грешка: {e}")
        return False


# ─── Публичен интерфейс ───────────────────────────────────────────────────────

def send_message(
    text: str,
    *,
    channels: list[str] | None = None,
) -> dict[str, bool]:
    """
    Изпраща съобщение към всички конфигурирани канали.

    Args:
        text:     Текстът на съобщението.
        channels: Списък от канали ['telegram'].
                  По подразбиране — всички налични.

    Returns:
        dict с резултат за всеки канал: {'telegram': True}
    """
    results: dict[str, bool] = {}

    if channels is None:
        channels = ["telegram"]

    if "telegram" in channels:
        token = resolve_setting("GENESIS_TELEGRAM_TOKEN")
        chat_id = resolve_setting("GENESIS_TELEGRAM_CHAT_ID")
        if token and chat_id:
            results["telegram"] = _send_telegram(text, token, chat_id)
        else:
            results["telegram"] = False  # Не е конфигуриран

    return results


def notify(text: str) -> bool:
    """
    Удобен helper — праща до всички конфигурирани канали, безопасно за
    извикване отвсякъде (никога не хвърля). Връща True ако поне един канал
    е приел съобщението.
    """
    try:
        results = send_message(text)
        return any(results.values())
    except Exception:
        return False


if __name__ == "__main__":
    # Тест (нужни са env vars)
    r = send_message("🤖 Genesis notifier тест!")
    print(f"Резултати: {r}")
    if not any(r.values()):
        print("⚠️  Нито един канал не е конфигуриран.")
        print("   Задайте: GENESIS_TELEGRAM_TOKEN + GENESIS_TELEGRAM_CHAT_ID")
