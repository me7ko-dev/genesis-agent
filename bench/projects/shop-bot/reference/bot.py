"""Еталонно решение — доказва, че скритите тестове се минават. Genesis не го вижда."""
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

HELP = ("Команди:\n/add хляб, мляко — добавя\n/list — показва списъка\n"
        "/del 2 — маха втория\n/clear — изчиства всичко")


def _load(path):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


def handle(update, store_path):
    message = update.get("message") or {}
    text = message.get("text")
    if not isinstance(text, str) or "chat" not in message:
        return None
    chat = message["chat"]["id"]
    command, _, arg = text.strip().partition(" ")
    command = command.split("@", 1)[0].lower()
    data = _load(store_path)
    items = data.get(str(chat), [])

    if command == "/start":
        reply = "Здравей! Пазя общ списък за пазаруване.\n" + HELP
    elif command == "/add":
        new = [x.strip() for x in arg.split(",") if x.strip()]
        if not new:
            reply = "Напиши какво да добавя: /add хляб, мляко"
        else:
            items += new
            reply = "Добавих: " + ", ".join(new)
    elif command == "/list":
        reply = "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1)) or "Списъкът е празен."
    elif command == "/del":
        arg = arg.strip()
        if arg.isdigit() and 1 <= int(arg) <= len(items):
            reply = "Махнах: " + items.pop(int(arg) - 1)
        else:
            reply = f"Няма такъв номер. Напиши /del и номер от 1 до {len(items)}." if items else "Списъкът е празен."
    elif command == "/clear":
        items = []
        reply = "Списъкът е изчистен."
    else:
        reply = "Не знам тази команда.\n" + HELP
    data[str(chat)] = items
    _save(store_path, data)
    return {"chat_id": chat, "text": reply}


def _api(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, body, timeout=60) as r:
        return json.load(r)


def main():
    token = os.environ["TELEGRAM_TOKEN"]
    store = os.environ.get("BOT_STORE", "lists.json")
    offset = 0
    while True:
        try:
            updates = _api(token, "getUpdates", {"offset": offset, "timeout": 50}).get("result", [])
        except OSError:
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            reply = handle(upd, store)
            if reply:
                _api(token, "sendMessage", reply)


if __name__ == "__main__":
    main()
