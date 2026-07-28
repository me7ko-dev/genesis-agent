#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Genesis Jarvis — гласов фронтенд към СЪЩОТО ядро (design note, 2026-07-27).

Четвъртият фронтенд (след терминала, Discord бота, GTK чата) към
genesis_agent.agent_core.Core + run_tool_loop — говориш, Genesis реално изпълнява
(същите RUN_CMD/WRITE_FILE/USE_SKILL/browser/памет инструменти) и ти отговаря
на глас. Текстовият вход остава наличен — не всичко трябва да се казва на
глас (напр. дълъг код за поставяне), но основният режим е разговорен.

Аудио стек (design note, 2026-07-27):
  STT: faster-whisper, модел "small", CPU — 1-2с на изречение след първо
       зареждане (кешира се локално). Автоматично разпознаване на език —
       не е насила български, за да върви и с английски/смесени изречения,
       както навсякъде другаде в Genesis.
  TTS: edge-tts (Microsoft Edge "Read Aloud" — безплатно, без API ключ,
       реален невронен глас, конфигурируем през GENESIS_TTS_VOICE). Избран
       след espeak-ng (spd-say), който звучи осезаемо роботизирано —
       разликата се чува веднага. Компромисът е ~5с мрежова латентност на
       реплика (TLS connect overhead към Microsoft, не расте много с дължина
       на текста) — приемливо във фонова нишка, не блокира разговора.
       spd-say (espeak-ng) остава като ОФЛАЙН резерва, ако мрежата липсва
       или edge-tts гръмне — същата "винаги имай работещ fallback"
       философия като provider веригата на Brain.
  Запис: `arecord` (ALSA CLI) вместо python-sounddevice — последното иска
       системната библиотека libportaudio2, която не може да се инсталира
       без sudo парола в тази среда; arecord/aplay вече бяха налични.

Безопасност: sandbox CONFIRM операции минават през ВИЗУАЛЕН диалог, не гласово
да/не разпознаване — грешно разпознато "не" като "да" на опасна операция би
било реален риск, затова потвърждението остава детерминистично (клик), както
при GTK чата.
"""
from __future__ import annotations

import atexit
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, Gtk  # noqa: E402


def _find_project_root() -> Path:
    """Виж genesis_gui._find_project_root — override-ът е GENESIS_PROJECT_ROOT,
    не GENESIS_HOME (последното е директорията с конфигурацията)."""
    env = os.environ.get("GENESIS_PROJECT_ROOT")
    if env and (Path(env) / "genesis_agent").is_dir():
        return Path(env)
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _find_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
GUI_DIR = Path(__file__).resolve().parent
if str(GUI_DIR) not in sys.path:
    sys.path.insert(0, str(GUI_DIR))

from genesis_agent.agent_core import Core, run_tool_loop  # noqa: E402
from genesis_gui import CSS, MessageWidget, ToolWidget  # noqa: E402 — преизползваме widget-ите

APP_ID = "org.genesis.Jarvis"
# Гласът е конфигурируем: закован български глас караше Jarvis да говори
# български на всеки, независимо от езика на отговора. `edge-tts
# --list-voices` изброява наличните.
TTS_VOICE = os.environ.get("GENESIS_TTS_VOICE", "bg-BG-BorislavNeural")
TTS_FALLBACK_LANG = os.environ.get("GENESIS_TTS_LANG", "bg")
WHISPER_MODEL_SIZE = "small"
MIN_RECORDING_SEC = 0.5     # по-кратко от това почти сигурно е случаен клик, не реч
MAX_RECORDING_SEC = 120     # предпазен таван — да не тече запис вечно ако забравиш
TTS_MAX_CHARS = 2000        # много дълга реч е непрактична за слушане


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # пиктограми, емотикони, транспорт, добавки
    "\U00002600-\U000027BF"   # разни символи, dingbats (✅ ✨ ❗ и т.н.)
    "\U0001F1E6-\U0001F1FF"   # регионални индикатори (флагове)
    "\U0000FE0F"              # variation selector (прави символа "цветен")
    "\U00002B00-\U00002BFF"   # стрелки/звезди
    "]+"
)
_MD_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_ITALIC_RE = re.compile(r"(\*\*\*|\*\*|\*|___|__)([^*_]+?)\1")
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"[ \t]{2,}")


def _speakable_text(text: str) -> str:
    """Превръща отговора в естествен изговорим текст. Без това TTS-ът четеше
    markdown синтаксиса буквално: "звезда звезда Готово звезда звезда",
    хаштагове, обратни кавички, описания на emoji.
    Маха ```код``` блокове изцяло (безсмислено да се изговаря
    суров Python), после чисти markdown маркерите от останалата проза, без
    да маха самия текст вътре в тях."""
    parts = MessageWidget._split_code(text)
    prose = "\n".join(chunk.strip() for is_code, chunk in parts if not is_code and chunk.strip())
    prose = prose or text

    prose = _EMOJI_RE.sub("", prose)
    prose = _URL_RE.sub("линк", prose)
    prose = _MD_CODE_SPAN_RE.sub(r"\1", prose)          # `нещо` → нещо
    prose = _MD_BOLD_ITALIC_RE.sub(r"\2", prose)         # **нещо**/*нещо* → нещо
    prose = _MD_HEADER_RE.sub("", prose)                 # ## Заглавие → Заглавие
    prose = _MD_BULLET_RE.sub("", prose)                 # "- т." / "* т." → т.
    prose = _MD_NUMBERED_RE.sub("", prose)               # "1. т." → т.
    prose = prose.replace("\n", ". ")
    prose = _WS_RE.sub(" ", prose)
    prose = re.sub(r"\.\s*\.+", ".", prose)              # двойни точки от съседни редове
    return prose.strip()[:TTS_MAX_CHARS]


class Recorder:
    """Тънка обвивка над `arecord` — записва до WAV, докато не бъде спрян."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._path: Path | None = None
        self._t0 = 0.0

    def start(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="genesis_jarvis_")
        import os

        os.close(fd)
        self._path = Path(path)
        self._t0 = time.time()
        self._proc = subprocess.Popen(
            ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", str(self._path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def stop(self) -> tuple[Path | None, float]:
        """Спира записа, връща (път_до_wav, продължителност_сек)."""
        dur = time.time() - self._t0
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        path, self._proc = self._path, None
        return path, dur

    @property
    def recording(self) -> bool:
        return self._proc is not None


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, core: Core) -> None:
        super().__init__(application=app, title="Genesis Jarvis")
        self.core = core
        self.busy = False
        self.muted = False
        self.recorder = Recorder()
        self._whisper: Any = None  # зарежда се лениво/на фон, виж _load_whisper
        self.set_default_size(760, 860)

        self.messages: list[dict] = [{"role": "system", "content": core.system_prompt}]

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="Genesis Jarvis", subtitle="зарежда…")
        header.set_title_widget(self.title_widget)

        self.mute_btn = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic")
        self.mute_btn.set_tooltip_text("Заглуши гласовия отговор")
        self.mute_btn.connect("toggled", self._on_mute_toggled)
        header.pack_start(self.mute_btn)

        menu = Gio.Menu()
        menu.append("Нов разговор", "win.clear")
        menu.append("Задачи и решения", "win.tasks")
        btn_menu = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(btn_menu)
        toolbar.add_top_bar(header)

        for name, cb in (("clear", self.on_clear), ("tasks", self.on_tasks)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

        # Транскрипт (преизползва MessageWidget/ToolWidget от GTK чата).
        self.chat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.chat.set_margin_top(8)
        self.chat.set_margin_bottom(8)
        self.scroll = Gtk.ScrolledWindow(vexpand=True)
        self.scroll.set_child(self.chat)

        # Голям кръгъл mic бутон — основният начин на взаимодействие.
        self.mic_btn = Gtk.Button()
        self.mic_btn.set_size_request(110, 110)
        self.mic_btn.add_css_class("circular")
        self.mic_btn.add_css_class("suggested-action")
        self.mic_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        self.mic_icon.set_pixel_size(40)
        self.mic_btn.set_child(self.mic_icon)
        self.mic_btn.connect("clicked", self._on_mic_clicked)

        self.status_label = Gtk.Label(label="Зареждам речевия модел…", css_classes=["status-dim"])
        self.spinner = Gtk.Spinner()

        mic_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        mic_box.set_margin_top(10)
        mic_box.set_margin_bottom(10)
        mic_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        mic_row.append(self.mic_btn)
        mic_box.append(mic_row)
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.CENTER)
        status_row.append(self.spinner)
        status_row.append(self.status_label)
        mic_box.append(status_row)

        # Текстов вход остава наличен — не всичко е удобно да се каже на глас.
        self.entry = Gtk.Entry(placeholder_text="…или пиши тук вместо да говориш")
        self.entry.connect("activate", lambda *_: self._submit_text())
        text_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        text_row.set_margin_start(10)
        text_row.set_margin_end(10)
        text_row.set_margin_bottom(10)
        text_row.append(self.entry)
        self.entry.set_hexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.scroll)
        box.append(mic_box)
        box.append(text_row)
        toolbar.set_content(box)
        self.set_content(toolbar)

        global _WINDOW
        _WINDOW = self

        if not core.ok:
            self.add_widget(MessageWidget(
                "Грешка при зареждане",
                "Genesis ядрото не можа да се зареди:\n\n```\n" + core.error[-1500:] + "\n```",
                kind="error"))
            self._set_ready(False)
            self.status_label.set_label("Ядрото не се зареди")
            return

        if core.briefing:
            self.add_widget(MessageWidget("📋 Оттук продължаваме", core.briefing[:2000], kind="assistant"))
        else:
            self.add_widget(MessageWidget(
                "Genesis", "Здравей. Натисни микрофона и говори — или пиши, ако е по-удобно.",
                kind="assistant"))

        self.connect("close-request", self.on_close)
        self._set_ready(False)
        threading.Thread(target=self._load_whisper, daemon=True).start()

    # ── Whisper (зарежда се веднъж, на фон — 30-40с първи път, ~1с от кеш) ──
    def _load_whisper(self) -> None:
        try:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            GLib.idle_add(self._set_ready, True)
        except Exception as e:
            GLib.idle_add(self.status_label.set_label, f"⚠ Гласовият вход не е наличен: {e}")

    def _set_ready(self, ready: bool) -> None:
        self.mic_btn.set_sensitive(ready)
        if ready:
            self.status_label.set_label("Готов — натисни и говори")
            self.title_widget.set_subtitle("готов")

    # ── UI помощници ─────────────────────────────────────────────────────────
    def add_widget(self, w: Gtk.Widget) -> None:
        self.chat.append(w)
        GLib.idle_add(self._scroll_bottom)

    def _scroll_bottom(self) -> bool:
        adj = self.scroll.get_vadjustment()
        adj.set_value(adj.get_upper())
        return False

    def set_status(self, text: str) -> None:
        self.status_label.set_label(text)
        self.title_widget.set_subtitle(text)

    def _on_mute_toggled(self, btn: Gtk.ToggleButton) -> None:
        self.muted = btn.get_active()
        btn.set_icon_name("audio-volume-muted-symbolic" if self.muted else "audio-volume-high-symbolic")

    # ── Микрофон: клик стартира, клик спира и праща за транскрипция ─────────
    def _on_mic_clicked(self, *_a) -> None:
        if self.busy:
            return
        if not self.recorder.recording:
            self.recorder.start()
            self.mic_btn.remove_css_class("suggested-action")
            self.mic_btn.add_css_class("destructive-action")
            self.mic_icon.set_from_icon_name("media-playback-stop-symbolic")
            self.set_status("🔴 Слушам… (натисни пак да спреш)")
            GLib.timeout_add_seconds(MAX_RECORDING_SEC, self._auto_stop_guard)
            return
        self._stop_and_transcribe()

    def _auto_stop_guard(self) -> bool:
        if self.recorder.recording:
            self._stop_and_transcribe()
        return False

    def _stop_and_transcribe(self) -> None:
        self.mic_btn.remove_css_class("destructive-action")
        self.mic_btn.add_css_class("suggested-action")
        self.mic_icon.set_from_icon_name("audio-input-microphone-symbolic")
        path, dur = self.recorder.stop()
        if path is None or dur < MIN_RECORDING_SEC:
            self.set_status("Готов — натисни и говори")
            if path:
                path.unlink(missing_ok=True)
            return
        self.busy = True
        self.mic_btn.set_sensitive(False)
        self.spinner.start()
        self.set_status("Разпознавам речта…")
        threading.Thread(target=self._transcribe_worker, args=(path,), daemon=True).start()

    def _transcribe_worker(self, wav_path: Path) -> None:
        try:
            segments, info = self._whisper.transcribe(str(wav_path))
            text = " ".join(s.text for s in segments).strip()
        except Exception as e:
            text = ""
            GLib.idle_add(self.set_status, f"⚠ Грешка при разпознаване: {e}")
        finally:
            wav_path.unlink(missing_ok=True)

        if not text:
            GLib.idle_add(self._transcribe_empty)
            return
        GLib.idle_add(self._on_transcribed, text)

    def _transcribe_empty(self) -> None:
        self.busy = False
        self.mic_btn.set_sensitive(True)
        self.spinner.stop()
        self.set_status("Не чух нищо разбираемо — натисни и опитай пак")

    def _on_transcribed(self, text: str) -> None:
        self.add_widget(MessageWidget("Ти 🎤", text, kind="user"))
        self._send(text)

    # ── Текстов вход (алтернатива на гласа) ──────────────────────────────────
    def _submit_text(self) -> None:
        text = self.entry.get_text().strip()
        if not text or self.busy:
            return
        self.entry.set_text("")
        self.add_widget(MessageWidget("Ти", text, kind="user"))
        self._send(text)

    # ── Изпращане към ядрото (общо за глас и текст) ──────────────────────────
    def _send(self, text: str) -> None:
        self.busy = True
        self.mic_btn.set_sensitive(False)
        self.spinner.start()
        self.set_status("Мисля…")
        self.messages.append({"role": "user", "content": text})
        self.core.remember("user", text)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        spoken: list[str] = []

        def on_assistant(text: str, prov: str, model: str) -> None:
            GLib.idle_add(self.add_widget, MessageWidget("Genesis", text, kind="assistant"))
            if text.strip():
                spoken.append(text)

        def on_tool_result(name: str, result: str) -> None:
            GLib.idle_add(self.add_widget, ToolWidget(name, result))

        def on_status(text: str) -> None:
            GLib.idle_add(self.set_status, text)

        try:
            self.messages = run_tool_loop(
                self.core, self.messages,
                on_assistant=on_assistant, on_tool_result=on_tool_result, on_status=on_status,
            )
        except Exception:
            GLib.idle_add(self.add_widget, MessageWidget(
                "Грешка", "```\n" + traceback.format_exc()[-1200:] + "\n```", kind="error"))
        finally:
            GLib.idle_add(self._done, spoken)

    def _done(self, spoken: list[str]) -> None:
        self.busy = False
        self.mic_btn.set_sensitive(True)
        self.spinner.stop()
        self.set_status("Готов — натисни и говори")
        if spoken and not self.muted:
            final_text = spoken[-1]
            threading.Thread(target=self._speak, args=(final_text,), daemon=True).start()

    # ── Говорене: edge-tts (естествен глас) → spd-say офлайн резерва ─────────
    def _speak(self, text: str) -> None:
        speakable = _speakable_text(text)
        if not speakable:
            return
        ok, why = self._speak_edge_tts(speakable)
        if ok:
            GLib.idle_add(self.set_status, "🌐 говоря (edge-tts)")
            return
        # ВИДИМОСТ (design note, 2026-07-27): по-рано и двата пътя гълтаха грешките
        # тихо — ако edge-tts падне (мрежа/DNS/каквото и да е), няма никаква
        # следа КОЯ причина е, само внезапно се чува спd-say пак и изглежда
        # като необясним регрес. Сега причината се вижда в лога и в статус
        # лентата, вместо да гадаем.
        print(f"[jarvis] edge-tts не проработи ({why}) → офлайн резерва (spd-say)",
              file=sys.stderr)
        GLib.idle_add(self.set_status, f"📻 говоря (офлайн резерва — edge-tts: {why})")
        self._speak_fallback(speakable)

    def _speak_edge_tts(self, text: str) -> tuple[bool, str]:
        """Естественият невронен глас — иска мрежа (Microsoft Edge Read
        Aloud, безплатно, без ключ). Връща (успех, причина_при_провал)."""
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="genesis_jarvis_say_")
        import os as _os
        _os.close(fd)
        try:
            # sys.executable, НЕ "python3": в pipx/venv инсталация `python3` е
            # системният интерпретатор, който няма edge_tts — TTS-ът се
            # проваляше винаги и тихо падаше на роботизирания spd-say.
            r = subprocess.run(
                [sys.executable, "-m", "edge_tts", "--voice", TTS_VOICE,
                 "--text", text, "--write-media", path],
                capture_output=True, timeout=20, text=True,
            )
            if r.returncode != 0:
                return False, (r.stderr or r.stdout or f"rc={r.returncode}").strip()[:200]
            if Path(path).stat().st_size == 0:
                return False, "празен mp3 файл"
            play = subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", path],
                                  capture_output=True, timeout=60, text=True)
            if play.returncode != 0:
                # mp3-ят е реален (сигурен признак, че edge-tts е успял), но
                # възпроизвеждането е гръмнало — това НЕ е причина да минаваме
                # на spd-say (той пак ще мине през същото аудио устройство),
                # просто докладваме и продължаваме, вместо да лъжем за успех.
                print(f"[jarvis] edge-tts генерира глас, но ffplay гръмна: "
                     f"{(play.stderr or '')[:200]}", file=sys.stderr)
            return True, ""
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"[:200]
        finally:
            Path(path).unlink(missing_ok=True)

    def _speak_fallback(self, text: str) -> None:
        try:
            subprocess.run(["spd-say", "-l", TTS_FALLBACK_LANG, "-w", text],
                           capture_output=True, timeout=60)
        except Exception as e:
            print(f"[jarvis] и офлайн резервата гръмна: {e}", file=sys.stderr)

    # ── Меню действия ─────────────────────────────────────────────────────────
    def on_clear(self, *_a) -> None:
        self.messages = [{"role": "system", "content": self.core.system_prompt}]
        child = self.chat.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.chat.remove(child)
            child = nxt
        self.add_widget(MessageWidget("Genesis", "Нов разговор.", kind="assistant"))

    def on_tasks(self, *_a) -> None:
        if not self.core.wm:
            self.add_widget(MessageWidget("Задачи", "workspace_memory не е наличен.", kind="error"))
            return
        b = self.core.wm.briefing(max_threads=20, max_decisions=10)
        self.add_widget(MessageWidget("📋 Задачи и решения", b or "Още нищо не е записано.", kind="assistant"))

    def on_close(self, *_a) -> bool:
        if self.recorder.recording:
            self.recorder.stop()
        try:
            if self.core.wm:
                convo = [m for m in self.messages if m.get("role") in ("user", "assistant")]
                if len(convo) >= 2:
                    self.core.wm.auto_capture(convo)
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox потвърждение — ВИЗУАЛЕН диалог, нарочно не гласово да/не (виж модул docstring).
# ─────────────────────────────────────────────────────────────────────────────
_WINDOW: Window | None = None


def _gui_confirm(operation: str, verdict) -> bool:
    if _WINDOW is None:
        return False
    done = threading.Event()
    answer = {"ok": False}

    def show() -> bool:
        reasons = "\n".join(f"• {r}" for r in getattr(verdict, "reasons", []))
        body = f"{reasons}\n\nОперация:\n{operation[:600]}"
        Dialog = getattr(Adw, "AlertDialog", None)
        if Dialog is not None:
            dlg = Dialog(heading="Genesis иска потвърждение", body=body)
            dlg.add_response("no", "Откажи")
            dlg.add_response("yes", "Изпълни")
            dlg.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("no")
            dlg.set_close_response("no")

            def on_response_alert(_d, r):
                answer.update(ok=r == "yes")
                done.set()
            dlg.connect("response", on_response_alert)
            dlg.present(_WINDOW)
        else:
            dlg = Adw.MessageDialog(transient_for=_WINDOW, heading="Genesis иска потвърждение", body=body)
            dlg.add_response("no", "Откажи")
            dlg.add_response("yes", "Изпълни")
            dlg.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("no")
            dlg.set_close_response("no")

            def on_response_message(_d, r):
                answer.update(ok=r == "yes")
                done.set()
            dlg.connect("response", on_response_message)
            dlg.present()
        return False

    GLib.idle_add(show)
    if not done.wait(timeout=300):
        return False
    return answer["ok"]


class App(Adw.Application):
    def __init__(self, core: Core) -> None:
        super().__init__(application_id=APP_ID)
        self.core = core

    def do_activate(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = Window(self, self.core)
        win.present()


def _kill_orphan_recorder() -> None:
    """Спира `arecord`, ако процесът приключва с активен запис — открито на
    живо (design note, 2026-07-27): убит отвън процес (SIGTERM, logout, killed от
    task manager) оставяше `arecord` да пише вечно в /tmp, държейки микрофона
    зает. GTK's "close-request" покрива чистото затваряне, но не и убиване
    отвън — затова и atexit, и явни signal handler-и за SIGTERM/SIGINT."""
    if _WINDOW is not None and _WINDOW.recorder.recording:
        _WINDOW.recorder.stop()


def _on_signal(signum, _frame) -> None:
    _kill_orphan_recorder()
    sys.exit(0)


def main() -> None:
    core = Core()
    try:
        from genesis_agent import sandbox

        sandbox.set_policy(sandbox.SandboxPolicy(mode="interactive", confirm_fn=_gui_confirm))
    except Exception:
        pass
    atexit.register(_kill_orphan_recorder)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    app = App(core)
    app.run(sys.argv)


if __name__ == "__main__":
    main()
