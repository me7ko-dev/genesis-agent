import pytest
from bot import handle


def msg(text, chat=42, uid=1):
    return {"update_id": uid, "message": {"message_id": uid, "date": 1767225600,
                                          "chat": {"id": chat, "type": "private"},
                                          "from": {"id": 7, "is_bot": False, "first_name": "Ани"},
                                          "text": text}}


@pytest.fixture
def say(tmp_path):
    store = str(tmp_path / "lists.json")

    def _say(text, chat=42):
        reply = handle(msg(text, chat), store)
        assert reply is not None and reply["chat_id"] == chat
        assert isinstance(reply["text"], str) and reply["text"].strip()
        return reply["text"]
    _say.store = store
    return _say


def items(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip()[:1].isdigit()]


def test_start(say):
    assert "/add" in say("/start")


def test_add_list_del_clear(say):
    say("/add Хляб,  мляко ,сирене")
    assert items(say("/list")) == ["1. Хляб", "2. мляко", "3. сирене"]
    say("/del 2")
    assert items(say("/list")) == ["1. Хляб", "2. сирене"]
    say("/add яйца")
    assert items(say("/list")) == ["1. Хляб", "2. сирене", "3. яйца"]
    say("/clear")
    text = say("/list")
    assert items(text) == [] and "празен" in text.lower()


def test_empty_list(say):
    assert "празен" in say("/list").lower()


def test_each_chat_has_its_own_list(say):
    say("/add бира", chat=1)
    say("/add вода", chat=-100200300)          # група
    assert items(say("/list", chat=1)) == ["1. бира"]
    assert items(say("/list", chat=-100200300)) == ["1. вода"]


def test_commands_addressed_to_the_bot_in_a_group(say):
    """В група Telegram праща командата като /add@ИмеНаБота."""
    say("/add@PazarBot домати", chat=-5)
    assert items(say("/list@PazarBot", chat=-5)) == ["1. домати"]


@pytest.mark.parametrize("bad", ["/del", "/del 9", "/del 0", "/del две", "/foo", "/add", "/add  , ,"])
def test_bad_input_does_not_crash_or_change_the_list(say, bad):
    say("/add хляб")
    say(bad)
    assert items(say("/list")) == ["1. хляб"]


def test_survives_a_restart(say, tmp_path):
    say("/add кафе, захар")
    import importlib

    import bot
    importlib.reload(bot)
    reply = bot.handle(msg("/list"), say.store)
    assert items(reply["text"]) == ["1. кафе", "2. захар"]


@pytest.mark.parametrize("update", [
    {"update_id": 5, "edited_message": {"chat": {"id": 42}, "text": "/list"}},
    {"update_id": 6, "message": {"message_id": 6, "chat": {"id": 42}, "sticker": {"file_id": "x"}}},
    {"update_id": 7, "callback_query": {"id": "1", "data": "x"}},
])
def test_updates_without_text_get_no_reply(tmp_path, update):
    assert handle(update, str(tmp_path / "s.json")) is None
