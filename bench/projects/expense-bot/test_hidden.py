import re
from datetime import datetime, timezone

import pytest
from bot import ExpenseBot

OCT_5 = int(datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp())
_update_id = 0


def msg(text, chat=111, ts=OCT_5):
    global _update_id
    _update_id += 1
    m = {"message_id": _update_id, "date": ts,
         "chat": {"id": chat, "type": "private"}, "from": {"id": chat, "first_name": "Тест"}}
    if text is not None:
        m["text"] = text
    return {"update_id": _update_id, "message": m}


@pytest.fixture
def bot(tmp_path):
    return ExpenseBot(str(tmp_path / "e.db"))


def say(bot, text, **kw):
    r = bot.handle(msg(text, **kw))
    assert r["method"] == "sendMessage"
    assert r["chat_id"] == kw.get("chat", 111)
    return r["text"]


def amounts(text):
    """Every money amount in the reply, as cents, decimal point or comma."""
    return [int(a.replace(",", "").replace(".", "")) for a in re.findall(r"\d+[.,]\d{2}(?!\d)", text)]


def test_start(bot):
    assert say(bot, "/start").strip()


def test_add_and_sum(bot):
    for t in ("/add 12,50 храна хляб и мляко", "/add 7.5 Храна", "/add 30 транспорт такси"):
        assert not say(bot, t).startswith("⚠️"), t
    s = say(bot, "/sum")
    assert "€" in s
    assert s.index("транспорт") < s.index("храна")  # the biggest first
    assert "Храна" not in s
    assert {3000, 2000, 5000} <= set(amounts(s))


def test_two_decimals(bot):
    for _ in range(3):
        say(bot, "/add 0,1 кафе")
    s = say(bot, "/sum")
    assert 30 in amounts(s)
    assert "0.30000" not in s and "0,30000" not in s


@pytest.mark.parametrize("bad", ["/add 0 храна", "/add -5 храна", "/add abc храна",
                                 "/add 5", "/add", "/foo", "/add 5,5,5 храна"])
def test_invalid(bot, bad):
    say(bot, "/add 10 храна")
    assert say(bot, bad).startswith("⚠️")
    assert 1000 in amounts(say(bot, "/sum"))
    assert not ({500, 550} & set(amounts(say(bot, "/sum"))))


def test_chats_are_separate(bot):
    say(bot, "/add 10 храна", chat=1)
    say(bot, "/add 99 кино", chat=2)
    s1 = say(bot, "/sum", chat=1)
    assert "кино" not in s1 and 9900 not in amounts(s1)
    say(bot, "/undo", chat=2)
    assert 1000 in amounts(say(bot, "/sum", chat=1))
    assert "кино" not in say(bot, "/sum", chat=2)


def test_undo_removes_last(bot):
    say(bot, "/add 10 храна")
    say(bot, "/add 20 сметки")
    say(bot, "/undo")
    s = say(bot, "/sum")
    assert "сметки" not in s and 1000 in amounts(s)


def test_month_in_sofia_time(bot):
    sep_15 = int(datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp())
    # 22:30 UTC on 30 Sep is 01:30 on 1 Oct in Sofia (EEST, UTC+3)
    oct_1_sofia = int(datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc).timestamp())
    say(bot, "/add 40 септември", ts=sep_15)
    say(bot, "/add 15 октомври", ts=oct_1_sofia)
    s = say(bot, "/sum", ts=OCT_5)
    assert "октомври" in s and "септември" not in s
    assert 1500 in amounts(s) and 4000 not in amounts(s)
    s_sep = say(bot, "/sum", ts=sep_15)
    assert "септември" in s_sep and "октомври" not in s_sep


def test_command_with_bot_name(bot):
    say(bot, "/add@moiat_bot 8 храна")
    s = say(bot, "/sum@moiat_bot")
    assert "храна" in s and 800 in amounts(s)


def test_list_last_five(bot):
    for i in range(1, 8):
        say(bot, f"/add {i} разни покупка{i}")
    s = say(bot, "/list")
    for i in range(3, 8):
        assert f"покупка{i}" in s
    assert "покупка1" not in s and "покупка2" not in s


def test_no_text_no_reply(bot):
    assert bot.handle(msg(None)) is None


def test_persists(tmp_path):
    db = str(tmp_path / "p.db")
    ExpenseBot(db).handle(msg("/add 12,34 храна"))
    r = ExpenseBot(db).handle(msg("/sum"))
    assert 1234 in amounts(r["text"])
