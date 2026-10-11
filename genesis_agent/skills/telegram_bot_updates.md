---
name: telegram_bot_updates
category: domain
description: Telegram бот без библиотеки — update без текст → None, командата в група е
  /add@ИмеНаБота (махни @…), chat_id е отрицателен в група, getUpdates с offset = последният
  update_id + 1, датата е Unix UTC → месецът по Europe/Sofia през zoneinfo.
triggers:
- telegram бот
- telegram bot
- телеграм бот
- getupdates sendmessage
version: '1.0'
author: Genesis
last_updated: '2026-10-11T21:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-10-11 (bench shop-bot, 2/2 пуска):
в група Telegram праща командата като `/add@PazarBot домати` — и двата бота
сравняваха цялата дума с `/add` и мълчаха. Задачата не споменава групи, но
истинският Telegram го прави (Bot API, „Commands“: `/command@bot_username`).
Същото важи за всеки бот, четен от група (expense-bot).
Източник: https://core.telegram.org/bots/api (Update, Message, getUpdates) и
https://core.telegram.org/bots/features#commands.

## Python Код
```python
"""Telegram Bot API без библиотеки — update → команда → отговор.

Факти (core.telegram.org/bots/api):
- Update носи ЕДНО от: message, edited_message, channel_post, callback_query,
  my_chat_member… Команда е само в "message". edited_message → None: човек,
  който поправи стара "/add хляб", не иска хлябът да се добави втори път.
- Съобщение без "text" (снимка, стикер, нов член, закачено) → няма команда → None.
  Снимката има "caption", не "text".
- В група командата идва като "/add@ИмеНаБота хляб". Махни "@…" от ПЪРВАТА дума;
  ако е адресирана до ДРУГ бот (известно ти е своето име) — не е за теб.
- Командата е до първия интервал/нов ред; аргументите са остатъкът (може празен).
  Сравнявай без значение на главни/малки ("/ADD" е "/add").
- chat id: message["chat"]["id"]; в група е отрицателен (-5, -100123…). Пази
  списъците по str(chat_id) в JSON — ключовете в JSON са низове.
- getUpdates?offset=N&timeout=30: offset = последният update_id + 1, иначе
  същите update-и идват пак. Таймаутът на urllib > timeout на long polling-а.
- message["date"] е Unix време в UTC → месец/ден по България:
  datetime.fromtimestamp(ts, ZoneInfo("Europe/Sofia")). На Windows zoneinfo
  иска пакета tzdata.
- sendMessage: {"chat_id": …, "text": …}; text до 4096 знака.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def message_of(update: dict) -> dict | None:
    """Новото съобщение от човек в update-а, или None (редакция, callback…)."""
    msg = update.get("message")
    return msg if isinstance(msg, dict) else None


def parse_command(text: str | None, bot_username: str | None = None) -> tuple[str, str] | None:
    """'/add@PazarBot хляб, мляко' → ('/add', 'хляб, мляко'); не команда → None.

    bot_username (без @): команда, адресирана до друг бот, връща None."""
    if not text or not text.startswith("/"):
        return None
    head, _, rest = text.strip().partition(" ")
    if "\n" in head:
        head, _, more = head.partition("\n")
        rest = (more + " " + rest).strip()
    cmd, _, target = head.partition("@")
    if target and bot_username and target.lower() != bot_username.lower().lstrip("@"):
        return None
    return cmd.lower(), rest.strip()


def chat_key(message: dict) -> str:
    """Ключ за JSON хранилището: '-5', '123' — низ, не int."""
    return str(message["chat"]["id"])


def next_offset(updates: list[dict], offset: int | None) -> int | None:
    """offset за следващия getUpdates: последният update_id + 1."""
    ids = [u["update_id"] for u in updates if "update_id" in u]
    return max(ids) + 1 if ids else offset


def local_month(unix_ts: int, tz: str = "Europe/Sofia") -> str:
    """'2026-03' по местно време — 31.03 23:30 UTC е вече април в София."""
    return datetime.fromtimestamp(unix_ts, ZoneInfo(tz)).strftime("%Y-%m")


if __name__ == "__main__":
    assert parse_command("/add@PazarBot домати") == ("/add", "домати")
    assert parse_command("/list@PazarBot") == ("/list", "")
    assert parse_command("/ADD хляб, мляко") == ("/add", "хляб, мляко")
    assert parse_command("/add@OtherBot x", bot_username="PazarBot") is None
    assert parse_command("/add@pazarbot x", bot_username="@PazarBot") == ("/add", "x")
    assert parse_command("/add\nхляб") == ("/add", "хляб")
    assert parse_command("здравей") is None and parse_command(None) is None
    assert message_of({"update_id": 1, "callback_query": {"id": "x"}}) is None
    photo = {"update_id": 2, "message": {"chat": {"id": -5}, "photo": [], "caption": "/add x"}}
    msg = message_of(photo)
    assert msg is not None and parse_command(msg.get("text")) is None
    assert chat_key(msg) == "-5"
    assert next_offset([{"update_id": 7}, {"update_id": 9}], None) == 10
    assert next_offset([], 10) == 10
    from zoneinfo import ZoneInfoNotFoundError
    try:
        ZoneInfo("Europe/Sofia")
    except ZoneInfoNotFoundError:   # Windows без tzdata: правилото е горе, тук няма зони
        pass
    else:
        assert local_month(1774999800) == "2026-04"   # 2026-03-31 23:30 UTC
        assert local_month(1774999800, "UTC") == "2026-03"
    print("OK")
```
