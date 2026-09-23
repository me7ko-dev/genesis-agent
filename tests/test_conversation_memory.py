"""genesis_agent.conversation_memory — persistent chat history + auto-summary,
used by genesis_terminal_agent.py and agent_core.py. Zero
coverage before this file, including of the exact regression its own
docstring describes (2026-07-25): compacting to `threshold` instead of a
buffer under it made get_history() look "frozen" because every add past the
threshold immediately re-triggered a 1-message compression, net growth zero.
That's the primary thing under test here, not just the CRUD operations.

DB_PATH is bound at import time (see tests/conftest.py's module docstring),
so every test redirects the module's own DB_PATH attribute to a tmp file
rather than patching genesis_agent.config.DATA_DIR after the fact.
"""
from __future__ import annotations

import pytest

import genesis_agent.conversation_memory as cm


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "DB_PATH", str(tmp_path / "conversation_memory.db"))


class TestAddAndGetHistory:
    def test_round_trips_in_chronological_order(self) -> None:
        cm.add_message("user", "hi")
        cm.add_message("assistant", "hello")
        cm.add_message("user", "how are you")
        history = cm.get_history()
        assert history == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "how are you"},
        ]

    def test_get_history_respects_last_n(self) -> None:
        for i in range(5):
            cm.add_message("user", f"msg {i}")
        history = cm.get_history(last_n=2)
        assert [h["content"] for h in history] == ["msg 3", "msg 4"]

    def test_empty_db_returns_empty_list(self) -> None:
        assert cm.get_history() == []


class TestSummarizeOldContext:
    def test_below_threshold_does_nothing(self) -> None:
        for i in range(10):
            cm.add_message("user", f"msg {i}")
        cm.summarize_old_context(threshold=50)
        assert len(cm.get_history(last_n=1000)) == 10

    def test_above_threshold_compresses_into_one_summary_message(self) -> None:
        for i in range(12):
            cm.add_message("user", f"msg {i}")
        cm.summarize_old_context(threshold=10, keep=4)
        history = cm.get_history(last_n=1000)
        # 4 kept originals + 1 summary. The summary replaces the OLDEST
        # messages, so it sorts first — this assertion used to expect it last
        # and explained why in a comment ("INSERTed after the DELETE, so it
        # gets the highest id"), which described the implementation, not the
        # intent: the module's own comment said the insert was meant to
        # "запази хронологията". It now does. See
        # TestTheSummaryKeepsItsPlaceInTheConversation for what that buys.
        assert len(history) == 5
        assert history[0]["role"] == "system"
        assert "[Context summary]" in history[0]["content"]
        # The most recent originals must survive untouched, in order.
        assert [h["content"] for h in history[1:]] == ["msg 8", "msg 9", "msg 10", "msg 11"]

    def test_growth_is_not_erased_by_repeated_compression_near_the_threshold(self) -> None:
        """The exact bug the module's docstring documents: compacting to
        `threshold` (buffer=0) made every add past it re-trigger a 1-message
        compaction, so the count oscillated instead of growing. With the
        default buffer (keep = threshold - 20) it must actually grow."""
        threshold = 30
        for i in range(threshold + 5):
            cm.add_message("user", f"msg {i}")
            cm.summarize_old_context(threshold=threshold)

        count_after_first_batch = len(cm.get_history(last_n=1000))

        for i in range(10):
            cm.add_message("user", f"more {i}")
            cm.summarize_old_context(threshold=threshold)

        count_after_second_batch = len(cm.get_history(last_n=1000))
        assert count_after_second_batch > count_after_first_batch


class TestSummarizeKeepLargerThanHistory:
    """`keep` is a public parameter (the module docstring advertises it), and
    `keep >= total` used to break two different ways (fixed 2026-08-12), both
    reached by passing a perfectly reasonable-looking value.
    """

    def test_keep_equal_to_total_is_a_no_op_not_a_crash(self) -> None:
        """total - keep == 0 produced `DELETE ... WHERE id IN ()`, which is
        not valid SQL — an OperationalError out of a memory write."""
        for i in range(12):
            cm.add_message("user", f"msg {i}")
        cm.summarize_old_context(threshold=10, keep=12)
        history = cm.get_history(last_n=1000)
        assert len(history) == 12
        assert all("[Context summary]" not in h["content"] for h in history)

    def test_keep_larger_than_total_does_not_wipe_the_conversation(self) -> None:
        """total - keep < 0 became a negative LIMIT, which SQLite reads as NO
        limit — so it selected and deleted the ENTIRE history, the exact
        opposite of 'keep the last N'."""
        for i in range(12):
            cm.add_message("user", f"msg {i}")
        cm.summarize_old_context(threshold=10, keep=500)
        history = cm.get_history(last_n=1000)
        assert len(history) == 12
        assert [h["content"] for h in history] == [f"msg {i}" for i in range(12)]

    def test_keep_zero_still_compresses_everything(self) -> None:
        """The other end of the range must keep working: keep=0 legitimately
        means 'summarize all of it'."""
        for i in range(12):
            cm.add_message("user", f"msg {i}")
        cm.summarize_old_context(threshold=10, keep=0)
        history = cm.get_history(last_n=1000)
        assert len(history) == 1
        assert history[0]["role"] == "system"

    def test_negative_keep_is_treated_as_zero_not_as_unlimited(self) -> None:
        for i in range(12):
            cm.add_message("user", f"msg {i}")
        cm.summarize_old_context(threshold=10, keep=-5)
        history = cm.get_history(last_n=1000)
        assert len(history) == 1
        assert history[0]["role"] == "system"


class TestClearSession:
    def test_clears_all_messages(self) -> None:
        cm.add_message("user", "hi")
        cm.clear_session()
        assert cm.get_history() == []

    def test_usable_again_after_clearing(self) -> None:
        cm.add_message("user", "before")
        cm.clear_session()
        cm.add_message("user", "after")
        assert cm.get_history() == [{"role": "user", "content": "after"}]


class TestTheSummaryKeepsItsPlaceInTheConversation:
    """Резюмето замества най-старите съобщения, значи стои на ТЯХНОТО място.

    Вмъкваше се без id, а AUTOINCREMENT дава най-голямото свободно — тоест
    резюме на НАЙ-СТАРИТЕ съобщения се нареждаше като НАЙ-НОВОТО, защото
    `get_history` сортира по id. Моделът получаваше „[Context summary] 21
    messages…" СЛЕД последния въпрос на човека: разговорът му се поднасяше
    разбъркан точно когато е станал достатъчно дълъг, за да има значение.
    """

    def _fill(self, n: int) -> None:
        for i in range(n):
            cm.add_message("user" if i % 2 == 0 else "assistant", f"съобщение {i}")

    def test_the_summary_comes_first_not_last(self) -> None:
        self._fill(55)
        history = cm.get_history(last_n=200)
        assert history[0]["role"] == "system"
        assert "[Context summary]" in history[0]["content"]
        assert all("[Context summary]" not in m["content"] for m in history[1:])

    def test_the_kept_messages_stay_in_order_after_it(self) -> None:
        self._fill(55)
        kept = [m["content"] for m in cm.get_history(last_n=200)[1:]]
        numbers = [int(c.split()[-1]) for c in kept]
        assert numbers == sorted(numbers), numbers
        assert numbers[-1] == 54, "последното съобщение трябва да е най-новото"

    def test_the_newest_messages_are_the_last_two_even_after_compression(self) -> None:
        """Точно проверката, която прави e2e тестът: добавям две и очаквам да
        са на опашката. Докато резюмето падаше най-отзад, това беше невярно
        всеки път, когато компресията се задейства."""
        self._fill(54)
        cm.add_message("user", "въпрос")
        cm.add_message("assistant", "отговор")
        last_two = cm.get_history(last_n=200)[-2:]
        assert [m["content"] for m in last_two] == ["въпрос", "отговор"]

    def test_a_second_compression_does_not_bury_the_first_summary(self) -> None:
        self._fill(55)
        self._fill(55)
        history = cm.get_history(last_n=200)
        summaries = [i for i, m in enumerate(history) if "[Context summary]" in m["content"]]
        assert summaries, "резюметата изчезнаха"
        assert summaries == sorted(summaries)
        assert max(summaries) < len(history) - 1, "резюме не бива да е последното"


class TestTheSummaryKeepsWhatMatters:
    """Резюмето се праща наново при ВСЯКА заявка до края на сесията, затова
    съдържанието му е и въпрос на цена, и въпрос на памет.

    Измерено върху реалната база преди поправката: 21 съобщения се свиха до
    360 знака, които бяха ЕДНО И СЪЩО съобщение за грешка, повторено четири
    пъти — защото старата версия слепваше всичко и режеше първите 300 знака.
    Всяка реплика на човека от този блок изчезна.
    """

    def test_the_humans_messages_lead(self) -> None:
        out = cm._simple_summarize([
            {"role": "assistant", "content": "работя по въпроса"},
            {"role": "user", "content": "мигрирай billing към новото API"},
        ])
        assert out.index("user: мигрирай") < out.index("assistant: работя")

    def test_a_repeated_error_does_not_eat_the_whole_summary(self) -> None:
        err = "Error: цялата верига е изчерпана | последна: skip: no HF_TOKEN configured"
        out = cm._simple_summarize(
            [{"role": "user", "content": "мигрирай billing"}]
            + [{"role": "assistant", "content": err}] * 12
        )
        assert out.count("HF_TOKEN") == 1, out
        assert "мигрирай billing" in out

    def test_a_long_message_is_truncated_not_dropped(self) -> None:
        out = cm._simple_summarize([{"role": "user", "content": "х" * 500}])
        assert "х" * 50 in out
        assert len(out) < 300

    def test_many_messages_keep_the_first_and_the_last(self) -> None:
        """Началото казва с какво сме тръгнали, краят — докъде сме стигнали.
        Произволен отрязък от средата не казва нито едното."""
        msgs = [{"role": "user", "content": f"стъпка {i}"} for i in range(20)]
        out = cm._simple_summarize(msgs)
        assert "стъпка 0" in out
        assert "стъпка 19" in out
        assert "…" in out
        assert "стъпка 9" not in out

    def test_the_header_still_reports_the_real_count_and_roles(self) -> None:
        out = cm._simple_summarize([
            {"role": "user", "content": "а"}, {"role": "assistant", "content": "б"},
        ])
        assert out.startswith("[Context summary] 2 messages from roles: assistant, user")

    def test_empty_and_whitespace_messages_are_skipped(self) -> None:
        out = cm._simple_summarize([
            {"role": "user", "content": "   "},
            {"role": "user", "content": ""},
            {"role": "user", "content": "истинско съобщение"},
        ])
        assert "истинско съобщение" in out
        assert "| user:  |" not in out

    def test_a_block_with_no_user_messages_still_summarises(self) -> None:
        out = cm._simple_summarize([{"role": "assistant", "content": "само аз говорих"}])
        assert "само аз говорих" in out

    def test_it_stays_bounded_even_for_a_huge_block(self) -> None:
        msgs = [{"role": "user", "content": f"{i} " + "дума " * 200} for i in range(200)]
        assert len(cm._simple_summarize(msgs)) < 1400
