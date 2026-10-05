"""genesis_terminal_agent.main — чатът и командите му, отвън.

`main()` се кара с подготвен вход (каквото операторът би написал) и се
гледа какво се показва и какво се променя: историята, режимите, файловете.
Моделът, мрежата и банерът са подменени. Тестовете не знаят как е устроен
`main()` вътре — пазят поведението, докато се разделя на по-малки части.
"""
from __future__ import annotations

import io
import json
import os
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text

import genesis_terminal_agent as gta
from genesis_agent.version_info import Source, UpdateCheck

# Истинските, преди Repl да ги подмени (менюто и банерът имат свои тестове).
SHOW_AGENT_MENU = gta.show_agent_menu
PRINT_BANNER = gta.print_minimal_banner


class Repl:
    def __init__(self, monkeypatch, tmp_path: Path) -> None:
        self.mp = monkeypatch
        self.tmp = tmp_path
        self.inputs: list[str] = []
        self.answers: list[str] = []
        self.turns: list[tuple[list[dict], str]] = []
        self.captured: list[list[dict]] = []
        self.calls: list[str] = []
        self.buf = io.StringIO()
        con = Console(file=self.buf, width=200, color_system=None, highlight=False)
        monkeypatch.setattr(con, "input", self._answer)
        monkeypatch.setattr(gta, "console", con)

        monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path / "history")
        (tmp_path / "history").mkdir()
        monkeypatch.setattr(gta, "WORKSPACE", tmp_path / "ws")
        (tmp_path / "ws").mkdir()
        monkeypatch.setattr(gta, "_CODING_MODE", False)
        monkeypatch.setattr(gta, "_LOCAL_ONLY_MODEL", None)
        monkeypatch.setattr(gta, "current_provider", "groq")
        monkeypatch.setattr(gta, "current_model_id", "openai/gpt-oss-120b")
        monkeypatch.setattr(gta, "print_minimal_banner", lambda: self.calls.append("banner"))
        monkeypatch.setattr(gta, "show_status_bar", lambda: None)
        monkeypatch.setattr(gta, "show_agent_menu", lambda: self.calls.append("menu"))
        monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
        monkeypatch.setattr(gta, "run_turn", self._turn)
        monkeypatch.setattr("genesis_agent.chat_input.read_message", self._read)
        monkeypatch.setattr("genesis_agent.self_update.report_pending", lambda: None)
        monkeypatch.setattr("genesis_agent.model_check.needs_check", lambda: False)
        monkeypatch.setattr("genesis_agent.workspace_memory.auto_capture", self._capture)
        monkeypatch.setattr("genesis_agent.browser.close", lambda: self.calls.append("browser.close"))
        monkeypatch.delenv("GENESIS_BACKUP_DIR", raising=False)

    def _read(self, first_line, *a, **kw) -> str:
        if not self.inputs:
            raise EOFError
        return self.inputs.pop(0)

    def _answer(self, prompt: str = "", *a, **kw) -> str:
        self.calls.append(f"asked:{Text.from_markup(prompt).plain}")
        return self.answers.pop(0) if self.answers else ""

    def _turn(self, messages, user_input, ui):
        self.turns.append((list(messages), user_input))
        assert ui is gta.RICH_UI
        out = deque(messages, maxlen=messages.maxlen)
        out.append({"role": "user", "content": user_input})
        out.append({"role": "assistant", "content": f"отговор на {user_input}"})
        return out

    def _capture(self, messages):
        self.captured.append(list(messages))
        return {"threads": 2, "decisions": 1, "preferences": 0}

    def run(self, *inputs: str, answers: tuple[str, ...] = ()) -> str:
        self.inputs = list(inputs)
        self.answers = list(answers)
        gta.main()
        return self.buf.getvalue()


@pytest.fixture
def repl(monkeypatch, tmp_path) -> Repl:
    return Repl(monkeypatch, tmp_path)


# ── Чатът ────────────────────────────────────────────────────────────────────

class TestTheChat:
    @pytest.mark.parametrize("word", ["exit", "quit", "изход", "EXIT"])
    def test_an_exit_word_ends_it(self, repl, word) -> None:
        out = repl.run(word, "това не се чете")
        assert repl.inputs == ["това не се чете"]
        assert "Довиждане" in out

    def test_a_message_goes_to_the_model_with_the_history(self, repl) -> None:
        repl.run("първо", "второ", "exit")
        assert [u for _m, u in repl.turns] == ["първо", "второ"]
        assert repl.turns[0][0] == [{"role": "system", "content": "SYS"}]
        assert repl.turns[1][0][-1] == {"role": "assistant", "content": "отговор на първо"}

    def test_an_empty_line_is_skipped(self, repl) -> None:
        repl.run("", "здрасти", "exit")
        assert [u for _m, u in repl.turns] == ["здрасти"]

    def test_eof_ends_it_cleanly(self, repl) -> None:
        assert "Довиждане" in repl.run("здрасти")

    def test_ctrl_c_ends_it(self, repl, monkeypatch) -> None:
        def _interrupt(*a, **kw):
            raise KeyboardInterrupt
        monkeypatch.setattr("genesis_agent.chat_input.read_message", _interrupt)
        assert "Довиждане" in repl.run()

    def test_an_error_in_a_turn_is_shown_and_the_chat_goes_on(self, repl, monkeypatch) -> None:
        def _boom(messages, user_input, ui):
            if user_input == "първо":
                raise RuntimeError("нещо се счупи")
            return messages
        monkeypatch.setattr(gta, "run_turn", _boom)
        out = repl.run("първо", "второ", "exit")
        assert "⚠ нещо се счупи" in out
        assert repl.inputs == []

    def test_a_broken_status_line_does_not_stop_the_chat(self, repl, monkeypatch) -> None:
        def _boom():
            raise ValueError("лош статус")
        monkeypatch.setattr(gta, "show_status_bar", _boom)
        out = repl.run("здрасти", "exit")
        assert "⚠ статус: лош статус" in out
        assert [u for _m, u in repl.turns] == ["здрасти"]


class TestStartAndEnd:
    def test_the_briefing_is_shown_at_the_start(self, repl, monkeypatch) -> None:
        monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", "отворена нишка #3"))
        out = repl.run("exit")
        assert "Оттук продължаваме" in out and "отворена нишка #3" in out

    def test_a_pending_update_report_is_shown(self, repl, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.self_update.report_pending", lambda: "Обновено до abc1234.")
        assert "Обновено до abc1234." in repl.run("exit")

    def test_the_weekly_model_check_starts_in_the_background(self, repl, monkeypatch) -> None:
        import threading
        ran = threading.Event()
        monkeypatch.setattr("genesis_agent.model_check.needs_check", lambda: True)
        monkeypatch.setattr("genesis_agent.model_check.run_check", ran.set)
        repl.run("exit")
        assert ran.wait(5)

    def test_what_was_done_is_remembered_at_exit(self, repl) -> None:
        out = repl.run("здрасти", "exit")
        assert [m["content"] for m in repl.captured[0]] == ["здрасти", "отговор на здрасти"]
        assert "Запомнено: 2 нишки, 1 решения, 0 предпочитания" in out
        assert "browser.close" in repl.calls

    def test_nothing_to_remember_without_a_conversation(self, repl) -> None:
        repl.run("exit")
        assert repl.captured == []


class TestRequestsThatAreCommands:
    def test_a_safe_one_runs_without_the_model(self, repl, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.skill_loader.format_skill_list", lambda: "• умение 1")
        out = repl.run("покажи уменията", "exit")
        assert "↪ /skills (без модел)" in out and "• умение 1" in out
        assert repl.turns == []

    def test_one_that_changes_something_asks_first(self, repl) -> None:
        repl.run("здрасти", "изчисти разговора", "още", "exit", answers=("",))
        assert any("Това е командата /clear" in c for c in repl.calls)
        assert repl.turns[-1][0] == [{"role": "system", "content": "SYS"}], "Enter = да"

    def test_no_sends_it_to_the_model(self, repl) -> None:
        repl.run("изчисти разговора", "exit", answers=("не",))
        assert [u for _m, u in repl.turns] == ["изчисти разговора"]


# ── Командите ────────────────────────────────────────────────────────────────

class TestSimpleCommands:
    @pytest.mark.parametrize("cmd", ["/model", "/agent", "/MODEL"])
    def test_the_model_menu(self, repl, cmd) -> None:
        repl.run(cmd, "exit")
        assert "menu" in repl.calls and repl.turns == []

    def test_clear_starts_a_new_conversation(self, repl) -> None:
        gta.count_usage({"prompt_tokens": 500, "completion_tokens": 5}, [], "")
        repl.run("първо", "/clear", "второ", "exit")
        assert repl.turns[1][0] == [{"role": "system", "content": "SYS"}]
        assert repl.calls.count("banner") == 2
        assert gta.total_input_tokens == 0

    def test_help_lists_the_commands(self, repl) -> None:
        out = repl.run("/help", "exit")
        for cmd in ("/model", "/maxcoding", "/history", "/backup", "/pack", "/update", "/tasks", "/done"):
            assert cmd in out

    def test_status(self, repl) -> None:
        out = repl.run("/status", "exit")
        assert "Модел: openai/gpt-oss-120b" in out
        assert "Доставчик: ⚡ Groq" in out
        assert "Токени за сесията" in out

    def test_status_in_local_mode(self, repl, monkeypatch) -> None:
        monkeypatch.setattr(gta, "_LOCAL_ONLY_MODEL", "qwen3:14b")
        out = repl.run("/status", "exit")
        assert "Локален режим — qwen3:14b" in out

    def test_models_shows_the_chain_and_the_active_one(self, repl, monkeypatch) -> None:
        monkeypatch.setattr(gta, "FALLBACK_CHAIN", [{"provider": "groq", "model": "openai/gpt-oss-120b"},
                                                   {"provider": "непознат", "model": "x"}])
        out = repl.run("/models", "exit")
        assert "FALLBACK CHAIN — 2 модела" in out
        assert "◀ ACTIVE" in out and "непознат" in out

    def test_skills_failure_is_shown(self, repl, monkeypatch) -> None:
        def _boom():
            raise OSError("няма папка")
        monkeypatch.setattr("genesis_agent.skill_loader.format_skill_list", _boom)
        assert "⚠ няма папка" in repl.run("/skills", "exit")


class TestModes:
    def test_maxcoding_toggles(self, repl, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.brain._load_coding_chain",
                            lambda: [{"provider": "nvidia", "model": "силен"}])
        out = repl.run("/maxcoding", "exit")
        assert gta._CODING_MODE is True
        assert "Кодинг режим ВКЛЮЧЕН" in out and "nvidia/силен" in out
        assert "Ръчно избраният модел" in out
        repl.buf.truncate(0)
        out = repl.run("/maxcoding", "exit")
        assert gta._CODING_MODE is False and "ИЗКЛЮЧЕН" in out

    @pytest.mark.parametrize(("cmd", "tier"), [("/local_model_max", "LOCAL_TIER_MAX"),
                                               ("/local_model_normal", "LOCAL_TIER_NORMAL")])
    def test_local_mode_toggles(self, repl, monkeypatch, cmd, tier) -> None:
        from genesis_agent import brain
        applied: list = []
        monkeypatch.setattr("genesis_agent.brain.set_local_only", applied.append)
        out = repl.run(cmd, "exit")
        assert gta._LOCAL_ONLY_MODEL == getattr(brain, tier) and applied == [getattr(brain, tier)]
        assert "Локален режим ВКЛЮЧЕН" in out
        repl.run(cmd, "exit")
        assert gta._LOCAL_ONLY_MODEL is None and applied[-1] is None

    def test_the_other_local_tier_switches_instead_of_turning_off(self, repl, monkeypatch) -> None:
        from genesis_agent import brain
        monkeypatch.setattr("genesis_agent.brain.set_local_only", lambda m: None)
        repl.run("/local_model_max", "/local_model_normal", "exit")
        assert gta._LOCAL_ONLY_MODEL == brain.LOCAL_TIER_NORMAL


class TestBackup:
    def test_without_a_target_it_says_how_to_set_one(self, repl) -> None:
        out = repl.run("/backup", "exit")
        assert "GENESIS_BACKUP_DIR" in out

    def test_it_copies_the_workspace(self, repl, monkeypatch) -> None:
        seen = []

        def _backup(src, dest):
            seen.append((src, dest))
            return True, ""
        monkeypatch.setenv("GENESIS_BACKUP_DIR", str(repl.tmp / "bk"))
        monkeypatch.setattr(gta, "_backup_workspace", _backup)
        out = repl.run("/backup", "exit")
        assert seen == [(repl.tmp / "ws", repl.tmp / "bk")]
        assert "Архивирането завърши" in out

    def test_a_failure_is_shown(self, repl, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_BACKUP_DIR", str(repl.tmp / "bk"))
        monkeypatch.setattr(gta, "_backup_workspace", lambda s, d: (False, "дискът е пълен"))
        assert "Архивирането се провали: дискът е пълен" in repl.run("/backup", "exit")


class TestUpdate:
    def _check(self, monkeypatch, src, latest) -> list:
        requested: list = []
        monkeypatch.setattr("genesis_agent.version_info.check_update", lambda: UpdateCheck(src, latest))
        monkeypatch.setattr("genesis_agent.version_info.changelog", lambda *a: ["нова функция"])
        monkeypatch.setattr("genesis_agent.self_update.request_update",
                            lambda **kw: requested.append(kw))
        return requested

    def test_not_installed_from_git(self, repl, monkeypatch) -> None:
        self._check(monkeypatch, None, None)
        assert "не е инсталирано от git" in repl.run("/update", "exit")

    def test_github_does_not_answer(self, repl, monkeypatch) -> None:
        self._check(monkeypatch, Source("https://github.com/a/b", "1234567abc", "main"), None)
        assert "Не можах да питам GitHub" in repl.run("/update", "exit")

    def test_already_up_to_date(self, repl, monkeypatch) -> None:
        self._check(monkeypatch, Source("https://github.com/a/b", "1234567abc", "main"), "1234567abc")
        assert "Вече си на последното (1234567, main)" in repl.run("/update", "exit")

    def test_newer_but_declined(self, repl, monkeypatch) -> None:
        requested = self._check(monkeypatch, Source("https://github.com/a/b", "1234567abc", "main"), "89abcdef")
        out = repl.run("/update", "exit", answers=("не",))
        assert "1234567 → 89abcde" in out and "• нова функция" in out
        assert "Пропуснато" in out and requested == []

    def test_newer_and_accepted(self, repl, monkeypatch) -> None:
        requested = self._check(monkeypatch, Source("https://github.com/a/b", "1234567abc", "main"), "89abcdef")
        out = repl.run("/ъпдейт", "exit", answers=("да",))
        assert requested == [{"pid": os.getpid(), "url": "https://github.com/a/b", "ref": "main"}]
        assert "насрочено на заден план" in out


class TestPack:
    def test_a_missing_folder(self, repl) -> None:
        assert "Няма такава папка" in repl.run("/pack липсва", "exit")

    def test_the_workspace_is_packed(self, repl, monkeypatch) -> None:
        packed = []

        def _pack(root):
            packed.append(root)
            return "резултат"
        monkeypatch.setattr("genesis_agent.pack.pack", _pack)
        monkeypatch.setattr("genesis_agent.pack.summary", lambda res: f"ОТЧЕТ за {res}")
        out = repl.run("/pack", "exit")
        assert packed == [repl.tmp / "ws"] and "ОТЧЕТ за резултат" in out

    def test_a_subfolder_and_an_error(self, repl, monkeypatch) -> None:
        (repl.tmp / "ws" / "проект").mkdir()

        def _fail(root):
            raise OSError("няма място")
        monkeypatch.setattr("genesis_agent.pack.pack", _fail)
        out = repl.run("/pack проект", "exit")
        assert "Опаковам" in out and "❌ няма място" in out


class TestThreads:
    def test_done_needs_a_number(self, repl) -> None:
        assert "Дай номер" in repl.run("/done", "exit")

    @pytest.mark.parametrize(("cmd", "drop"), [("/done 3 4", False), ("/drop 3 4", True),
                                               ("/готово 3 4", False)])
    def test_done_and_drop(self, repl, monkeypatch, cmd, drop) -> None:
        closed = []

        def _close(ident, drop=False):
            closed.append((ident, drop))
            return f"#{ident} ок"
        monkeypatch.setattr("genesis_agent.workspace_memory.close_thread", _close)
        out = repl.run(cmd, "exit")
        assert closed == [("3", drop), ("4", drop)] and "#4 ок" in out

    def test_tasks_shows_the_briefing_and_counts(self, repl, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.workspace_memory.briefing", lambda **kw: "нишка #1")
        monkeypatch.setattr("genesis_agent.workspace_memory.stats",
                            lambda: {"open": 1, "blocked": 0, "done": 5})
        out = repl.run("/tasks", "exit")
        assert "1 отворени, 0 блокирани, 5 готови" in out and "нишка #1" in out

    def test_tasks_with_nothing_recorded(self, repl, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.workspace_memory.briefing", lambda **kw: "")
        monkeypatch.setattr("genesis_agent.workspace_memory.stats",
                            lambda: {"open": 0, "blocked": 0, "done": 0})
        assert "Още нищо не е записано" in repl.run("/задачи", "exit")


class TestHistory:
    def _sessions(self, repl, n: int) -> list[Path]:
        files = []
        for i in range(n):
            f = repl.tmp / "history" / f"session_2026100{i}_120000.json"
            f.write_text(json.dumps([{"role": "system", "content": "старо"},
                                     {"role": "user", "content": f"сесия {i}"}]), encoding="utf-8")
            os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
            files.append(f)
        return files

    def test_no_sessions(self, repl) -> None:
        assert "Няма намерена история" in repl.run("/history", "exit")

    def test_a_chosen_session_continues_with_the_current_prompt(self, repl) -> None:
        self._sessions(repl, 2)
        out = repl.run("/history", "продължи", "exit", answers=("1",))
        assert "Сесията е заредена" in out
        history = repl.turns[0][0]
        assert history[0] == {"role": "system", "content": "SYS"}
        assert history[-1] == {"role": "user", "content": "сесия 1"}

    def test_only_the_shown_ten_can_be_chosen(self, repl) -> None:
        self._sessions(repl, 12)
        out = repl.run("/history", "exit", answers=("11",))
        assert "Невалиден избор" in out

    def test_zero_goes_back_and_text_is_invalid(self, repl) -> None:
        self._sessions(repl, 1)
        out = repl.run("/history", "/history", "exit", answers=("0", "две"))
        assert out.count("Невалиден избор") == 1


# ── Менюто за модел ──────────────────────────────────────────────────────────

class TestTheModelMenu:
    @pytest.fixture
    def menu(self, monkeypatch, repl):
        monkeypatch.setattr(gta, "show_agent_menu", SHOW_AGENT_MENU)
        return repl

    def _pick(self, menu, monkeypatch, *answers: str) -> str:
        menu.answers = list(answers)
        gta.show_agent_menu()
        return menu.buf.getvalue()

    def test_nothing_happens_on_back_or_junk(self, menu, monkeypatch) -> None:
        for answer in ("0", "абв", "999"):
            self._pick(menu, monkeypatch, answer)
        assert gta.current_provider == "groq"

    def test_a_provider_that_is_not_ready_says_what_is_missing(self, menu, monkeypatch) -> None:
        monkeypatch.setitem(gta.KEYS, "GROQ_API_KEY", "")
        idx = list(gta.PROVIDERS).index("groq") + 1
        assert "не е готов: липсва GROQ_API_KEY" in self._pick(menu, monkeypatch, str(idx))

    def test_choosing_a_model(self, menu, monkeypatch) -> None:
        monkeypatch.setitem(gta.KEYS, "NVIDIA_API_KEY", "k")
        monkeypatch.setattr(gta, "fetch_models", lambda pk: ["a/модел-1", "b/модел-2"])
        idx = list(gta.PROVIDERS).index("nvidia") + 1
        out = self._pick(menu, monkeypatch, str(idx), "2")
        assert (gta.current_provider, gta.current_model_id) == ("nvidia", "b/модел-2")
        assert "✓ b/модел-2" in out

    def test_an_empty_model_list(self, menu, monkeypatch) -> None:
        monkeypatch.setitem(gta.KEYS, "NVIDIA_API_KEY", "k")
        monkeypatch.setattr(gta, "fetch_models", lambda pk: [])
        idx = list(gta.PROVIDERS).index("nvidia") + 1
        assert "Неуспешно" in self._pick(menu, monkeypatch, str(idx))

    @pytest.mark.parametrize(("answer", "pulled"), [("да", "llama3.2"), ("mistral", "mistral"), ("не", None)])
    def test_ollama_without_models_offers_a_pull(self, menu, monkeypatch, answer, pulled) -> None:
        started = []
        monkeypatch.setattr(gta, "fetch_models", lambda pk: ["__no_models__"])
        monkeypatch.setattr(gta.subprocess, "Popen", lambda argv: started.append(argv))
        idx = list(gta.PROVIDERS).index("ollama") + 1
        out = self._pick(menu, monkeypatch, str(idx), answer)
        assert "няма изтеглени модели" in out
        assert started == ([["ollama", "pull", pulled]] if pulled else [])


# ── Банерът и системната информация ──────────────────────────────────────────

class TestBanner:
    @pytest.fixture
    def banner(self, monkeypatch, repl):
        monkeypatch.setattr(gta, "print_minimal_banner", PRINT_BANNER)
        monkeypatch.setattr(gta.os, "system", lambda cmd: 0)
        return repl

    def test_it_shows_the_model_and_the_keys(self, banner, monkeypatch) -> None:
        monkeypatch.setattr(gta, "get_system_info", lambda: {"ollama": "❌ Не работи", "ram_pct": 95,
                                                             "ram": "15GB", "gpu": "RTX"})
        monkeypatch.setitem(gta.KEYS, "GROQ_API_KEY", "k")
        gta.print_minimal_banner()
        out = banner.buf.getvalue()
        assert "gpt-oss-120b" in out and "⚡ Groq" in out and "RTX" in out
        assert "Няма конфигуриран нито един API ключ" not in out

    def test_without_any_key_it_says_so(self, banner, monkeypatch) -> None:
        monkeypatch.setattr(gta, "get_system_info", lambda: {"ollama": "✅ Работи (2 модела)"})
        for k in list(gta.KEYS):
            if k != "OLLAMA_MODEL":
                monkeypatch.setitem(gta.KEYS, k, "")
        monkeypatch.setattr(gta, "_ollama_cloud_multi", {})
        gta.print_minimal_banner()
        out = banner.buf.getvalue()
        assert "Няма конфигуриран нито един API ключ" in out
        assert "Локалният Ollama е наличен" in out


class TestSystemInfo:
    def test_everything_missing_reads_as_unknown(self, monkeypatch) -> None:
        def _no_file(*a, **kw):
            raise OSError("няма /proc")
        monkeypatch.setattr("builtins.open", _no_file)
        monkeypatch.setattr(gta.requests, "get", lambda *a, **kw: SimpleNamespace(status_code=500))
        monkeypatch.setattr(gta.shutil, "which", lambda name: None)
        info = gta.get_system_info()
        assert info["cpu_load"] == "N/A" and info["ram"] == "N/A"
        assert info["ollama"] == "❌ Не работи" and info["ollama_ok"] is False
        assert info["gpu"] == "Няма NVIDIA GPU"
        assert "GB" in info["disk"]

    def test_ollama_with_models_and_a_gpu(self, monkeypatch) -> None:
        monkeypatch.setattr(gta.requests, "get", lambda *a, **kw: SimpleNamespace(
            status_code=200, json=lambda: {"models": [{"name": "a"}, {"name": "b"}]}))
        monkeypatch.setattr(gta.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(gta.subprocess, "check_output", lambda *a, **kw: "RTX 4070, 1000, 12000, 50")
        info = gta.get_system_info()
        assert info["ollama"] == "✅ Работи (2 модела)"
        assert info["gpu"] == "RTX 4070  1000MB/12000MB  50°C"
