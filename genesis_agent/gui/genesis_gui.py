#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Genesis Agent — нативно GTK4/libadwaita приложение за Linux.

Трети фронтенд към СЪЩОТО ядро (след терминалния чат и Discord бота). Не
дублира логика: provider веригата е genesis_agent.brain.Brain, изпълнението на
инструменти е genesis_skills (същите backend-и), паметта е споделената
workspace_memory/conversation_memory. Затова умение, записано от терминала,
се вижда тук веднага, и обратно.

Разлика от терминала, която ИМА значение за безопасността: там sandbox-ът
пита през console.input(), тук — през истински модален диалог. Режимът пак е
"interactive", т.е. CONFIRM операциите чакат реално решение на човек, не се
пускат тихо (виж _gui_confirm).

Пускане:  genesis-agent        (след инсталация)
          python3 genesis_agent/gui/genesis_gui.py   (от repo-то)
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gdk, Gio, Gtk  # noqa: E402

# ── Намиране на инсталацията на Genesis ──────────────────────────────────────
# GUI-то е фронтенд към СЪЩЕСТВУВАЩА инсталация (със скиловете и паметта на
# потребителя) — не носи свое копие, за да няма две разминаващи се библиотеки.
#
# ВАЖНО: override-ът е GENESIS_PROJECT_ROOT, НЕ GENESIS_HOME. Тук се четеше
# GENESIS_HOME, но paths.py и .env.example го дефинират като директорията с
# конфигурацията (~/.genesis, където е .env) — един и същ env var с две
# значения. Тръгналият по документацията получаваше `~/.genesis/genesis_agent`,
# което не съществува.
def _find_project_root() -> Path:
    env = os.environ.get("GENESIS_PROJECT_ROOT")
    if env and (Path(env) / "genesis_agent").is_dir():
        return Path(env)
    # .../<root>/genesis_agent/gui/this_file.py → <root>, which is the repo in
    # a checkout and site-packages in an install. Either way `import
    # genesis_agent` resolves from there.
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _find_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from genesis_agent.agent_core import Core, run_tool_loop  # noqa: E402

APP_ID = "org.genesis.Agent"
TOOL_ROUND_CAP = 8

CSS = """
.msg-user      { background: alpha(@accent_bg_color, .18); border-radius: 14px; padding: 10px 14px; }
.msg-assistant { background: alpha(@card_fg_color, .06);   border-radius: 14px; padding: 10px 14px; }
.msg-error     { background: alpha(@error_color, .16);     border-radius: 14px; padding: 10px 14px; }
.code-block    { background: alpha(@card_fg_color, .10); border-radius: 8px; padding: 10px;
                 font-family: monospace; font-size: 12px; }
.tool-out      { font-family: monospace; font-size: 11px; }
.speaker       { font-size: 11px; font-weight: bold; opacity: .65; }
.status-dim    { font-size: 11px; opacity: .6; }
"""


# Core (config + Brain + skills + памет) и run_tool_loop (пълния агентен
# цикъл) са в genesis_agent.agent_core — споделени с Jarvis гласовия фронтенд,
# за да не се дублира логика между фронтендите (виж модула за детайли).


# ─────────────────────────────────────────────────────────────────────────────
# Съобщения в чата
# ─────────────────────────────────────────────────────────────────────────────
def _label(text: str, *, selectable: bool = True, css: str | None = None) -> Gtk.Label:
    lb = Gtk.Label(label=text, xalign=0.0, wrap=True, selectable=selectable)
    lb.set_wrap_mode(2)  # WORD_CHAR — дългите пътища/URL не разпъват прозореца
    if css:
        lb.add_css_class(css)
    return lb


class MessageWidget(Gtk.Box):
    """Един ред в чата. Кодовите блокове се рендират отделно, с моноширинен шрифт."""

    def __init__(self, speaker: str, text: str, kind: str = "assistant") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(10)
        self.set_margin_end(10)

        self.append(_label(speaker, selectable=False, css="speaker"))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.add_css_class(f"msg-{kind}")
        for is_code, chunk in self._split_code(text):
            if not chunk.strip():
                continue
            if is_code:
                sw = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER)
                sw.set_child(_label(chunk.rstrip()))
                sw.add_css_class("code-block")
                body.append(sw)
            else:
                body.append(_label(chunk.strip()))
        self.append(body)

    @staticmethod
    def _split_code(text: str) -> list:
        """Разделя на (is_code, chunk) по ``` огради. Нечетен брой огради =
        незатворен блок — остатъкът пак се показва като код, не се губи."""
        parts: list = []
        buf: list[str] = []
        in_code = False
        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                parts.append((in_code, "\n".join(buf)))
                buf, in_code = [], not in_code
                continue
            buf.append(line)
        parts.append((in_code, "\n".join(buf)))
        return parts


class ToolWidget(Gtk.Box):
    """Резултат от инструмент — сгънат по подразбиране, за да не залива чата."""

    def __init__(self, name: str, result: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        first = (result.strip().split("\n") or [""])[0][:80]
        exp = Gtk.Expander(label=f"🔧 {name} — {first}")
        sw = Gtk.ScrolledWindow(max_content_height=280, propagate_natural_height=True)
        lb = _label(result[:8000])
        lb.add_css_class("tool-out")
        sw.set_child(lb)
        sw.add_css_class("code-block")
        exp.set_child(sw)
        self.append(exp)


# ─────────────────────────────────────────────────────────────────────────────
# Прозорецът
# ─────────────────────────────────────────────────────────────────────────────
class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, core: Core) -> None:
        super().__init__(application=app, title="Genesis Agent")
        self.core = core
        self.busy = False
        self.set_default_size(980, 720)

        self.messages: Any = deque(
            [{"role": "system", "content": core.system_prompt}], maxlen=30
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.subtitle = Gtk.Label(label="готов", css_classes=["status-dim"])
        title = Adw.WindowTitle(title="Genesis Agent", subtitle="готов")
        self.title_widget = title
        header.set_title_widget(title)

        menu = Gio.Menu()
        menu.append("Нов разговор", "win.clear")
        menu.append("Задачи и решения", "win.tasks")
        menu.append("Разход на токени", "win.budget")
        btn_menu = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(btn_menu)
        toolbar.add_top_bar(header)

        for name, cb in (
            ("clear", self.on_clear),
            ("tasks", self.on_tasks),
            ("budget", self.on_budget),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

        # Чат площ
        self.chat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.chat.set_margin_top(8)
        self.chat.set_margin_bottom(8)
        self.scroll = Gtk.ScrolledWindow(vexpand=True)
        self.scroll.set_child(self.chat)

        # Вход
        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.entry.set_size_request(-1, 46)
        entry_sw = Gtk.ScrolledWindow(max_content_height=160, propagate_natural_height=True)
        entry_sw.set_child(self.entry)
        entry_sw.add_css_class("card")

        self.send_btn = Gtk.Button(icon_name="document-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.connect("clicked", lambda *_: self.submit())

        self.spinner = Gtk.Spinner()
        self.spinner.set_valign(Gtk.Align.END)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.set_margin_start(10)
        bottom.set_margin_end(10)
        bottom.set_margin_top(4)
        bottom.set_margin_bottom(10)
        bottom.append(entry_sw)
        entry_sw.set_hexpand(True)
        bottom.append(self.spinner)
        bottom.append(self.send_btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.scroll)
        box.append(bottom)
        toolbar.set_content(box)
        self.set_content(toolbar)

        # Enter праща, Shift+Enter нов ред.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.entry.add_controller(keys)

        if not core.ok:
            self.add_widget(
                MessageWidget(
                    "Грешка при зареждане",
                    "Genesis ядрото не можа да се зареди:\n\n```\n"
                    + core.error[-1500:]
                    + "\n```",
                    kind="error",
                )
            )
            self.entry.set_sensitive(False)
            self.send_btn.set_sensitive(False)
            return

        if core.briefing:
            self.add_widget(
                MessageWidget("📋 Оттук продължаваме", core.briefing[:2500], kind="assistant")
            )
        else:
            self.add_widget(
                MessageWidget(
                    "Genesis",
                    "Здравей. Мога да пиша и изпълнявам код, да работя с терминала, "
                    "браузъра и файловете ти, и помня работата между сесиите.",
                    kind="assistant",
                )
            )
        self.connect("close-request", self.on_close)

    # ── UI помощници ─────────────────────────────────────────────────────────
    def add_widget(self, w: Gtk.Widget) -> None:
        self.chat.append(w)
        GLib.idle_add(self._scroll_bottom)

    def _scroll_bottom(self) -> bool:
        adj = self.scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def set_status(self, text: str) -> None:
        self.title_widget.set_subtitle(text)

    def on_key(self, _ctrl, keyval, _code, state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                return False
            self.submit()
            return True
        return False

    # ── Действия от менюто ───────────────────────────────────────────────────
    def on_clear(self, *_a) -> None:
        self.messages = deque(
            [{"role": "system", "content": self.core.system_prompt}], maxlen=30
        )
        child = self.chat.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.chat.remove(child)
            child = nxt
        self.add_widget(MessageWidget("Genesis", "Нов разговор.", kind="assistant"))

    def on_tasks(self, *_a) -> None:
        try:
            txt = self.core.wm.briefing() if self.core.wm else "Няма памет."
        except Exception as e:
            txt = f"Грешка: {e}"
        self.add_widget(MessageWidget("📋 Задачи", txt or "Нищо отворено.", kind="assistant"))

    def on_budget(self, *_a) -> None:
        try:
            from genesis_agent import budget

            t = budget.today_totals()
            txt = (
                f"Днес: {t.get('calls', 0)} обаждания · "
                f"{t.get('total_tokens', 0):,} токена\n"
                + "\n".join(
                    f"  {p}: {d.get('calls',0)} · {d.get('total_tokens',0):,}"
                    for p, d in (t.get("by_provider") or {}).items()
                )
            )
        except Exception as e:
            txt = f"Грешка: {e}"
        self.add_widget(MessageWidget("💰 Разход", txt, kind="assistant"))

    def on_close(self, *_a) -> bool:
        # Записваме каквото сме научили, преди прозорецът да изчезне — иначе
        # затварянето губи решенията от целия разговор (auto_capture е точно
        # за случая, в който моделът НЕ е викнал memory tool сам).
        try:
            if self.core.wm:
                convo = [m for m in self.messages if m.get("role") in ("user", "assistant")]
                if len(convo) >= 2:
                    self.core.wm.auto_capture(convo)
        except Exception:
            pass
        return False

    # ── Изпращане ────────────────────────────────────────────────────────────
    def submit(self) -> None:
        if self.busy:
            return
        buf = self.entry.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not text:
            return
        buf.set_text("")
        self.add_widget(MessageWidget("Ти", text, kind="user"))
        self.messages.append({"role": "user", "content": text})
        self.core.remember("user", text)
        self._start_work()

    def _start_work(self) -> None:
        self.busy = True
        self.spinner.start()
        self.send_btn.set_sensitive(False)
        self.set_status("мисли…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        """Целият tool цикъл живее в genesis_agent.agent_core.run_tool_loop (споделен
        с Jarvis) — тук само UI callback-ите, в отделна нишка, UI-ят не блокира."""
        def on_assistant(text: str, prov: str, model: str) -> None:
            GLib.idle_add(self.add_widget, MessageWidget("Genesis", text, kind="assistant"))

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
            GLib.idle_add(
                self.add_widget,
                MessageWidget(
                    "Грешка", "```\n" + traceback.format_exc()[-1200:] + "\n```", kind="error"
                ),
            )
        finally:
            GLib.idle_add(self._done)

    def _done(self) -> bool:
        self.busy = False
        self.spinner.stop()
        self.send_btn.set_sensitive(True)
        self.set_status("готов")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox потвърждение през диалог
# ─────────────────────────────────────────────────────────────────────────────
_WINDOW: Window | None = None


def _gui_confirm(operation: str, verdict) -> bool:
    """Извиква се от работната нишка → диалогът се показва в GTK нишката, а
    нишката чака решението. Без това CONFIRM операциите биха паднали на
    неинтерактивния "deny" път и нищо рисково не би могло да се одобри от GUI-то.
    """
    if _WINDOW is None:
        return False
    done = threading.Event()
    answer = {"ok": False}

    def show() -> bool:
        reasons = "\n".join(f"• {r}" for r in getattr(verdict, "reasons", []))
        body = f"{reasons}\n\nОперация:\n{operation[:600]}"
        Dialog = getattr(Adw, "AlertDialog", None)
        if Dialog is not None:  # libadwaita ≥ 1.5
            dlg = Dialog(heading="Genesis иска потвърждение", body=body)
            dlg.add_response("no", "Откажи")
            dlg.add_response("yes", "Изпълни")
            dlg.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("no")
            dlg.set_close_response("no")

            def on_resp(_d, resp):
                answer["ok"] = resp == "yes"
                done.set()

            dlg.connect("response", on_resp)
            dlg.present(_WINDOW)
        else:
            dlg = Adw.MessageDialog(
                transient_for=_WINDOW, heading="Genesis иска потвърждение", body=body
            )
            dlg.add_response("no", "Откажи")
            dlg.add_response("yes", "Изпълни")
            dlg.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("no")
            dlg.set_close_response("no")

            def on_resp2(_d, resp):
                answer["ok"] = resp == "yes"
                done.set()

            dlg.connect("response", on_resp2)
            dlg.present()
        return False

    GLib.idle_add(show)
    # Таймаут, за да не увисне нишката завинаги, ако диалогът бъде убит.
    if not done.wait(timeout=300):
        return False
    return answer["ok"]


class App(Adw.Application):
    def __init__(self, core: Core) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.core = core

    def do_activate(self) -> None:
        global _WINDOW
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = self.props.active_window or Window(self, self.core)
        _WINDOW = win
        if self.core.ok:
            try:
                from genesis_agent import sandbox

                sandbox.set_policy(
                    sandbox.SandboxPolicy(mode="interactive", confirm_fn=_gui_confirm)
                )
            except Exception:
                pass
        win.present()


def main() -> int:
    core = Core()
    return App(core).run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
