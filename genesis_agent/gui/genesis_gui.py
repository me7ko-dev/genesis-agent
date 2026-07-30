#!/usr/bin/env python3
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
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk


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

from genesis_agent.agent_core import Core, run_tool_loop
from genesis_agent.gui import gui_sessions

APP_ID = "org.genesis.Agent"
TOOL_ROUND_CAP = 8

_HELP_TEXT = (
    "/model — отвори избора на модел\n"
    "/clear — нов разговор\n"
    "/tasks — отворени нишки и решения от паметта\n"
    "/budget — разход на токени днес\n"
    "/done <id> — маркирай нишка от паметта завършена\n"
    "/drop <id> — изхвърли нишка от паметта\n"
    "/help — тази справка"
)

CSS = """
.msg-user      { background: alpha(@accent_bg_color, .18); border-radius: 14px; padding: 10px 14px; }
.msg-assistant { background: alpha(@card_fg_color, .06);   border-radius: 14px; padding: 10px 14px; }
.msg-error     { background: alpha(@error_color, .16);     border-radius: 14px; padding: 10px 14px; }
.code-block    { background: alpha(@card_fg_color, .10); border-radius: 8px; padding: 10px;
                 font-family: monospace; font-size: 12px; }
.tool-out      { font-family: monospace; font-size: 11px; }
.diff-add      { font-family: monospace; font-size: 11px; background: alpha(@success_color, .18); }
.diff-del      { font-family: monospace; font-size: 11px; background: alpha(@error_color, .16); }
.diff-hunk     { font-family: monospace; font-size: 11px; opacity: .55; font-style: italic; }
.speaker       { font-size: 11px; font-weight: bold; opacity: .65; }
.status-dim    { font-size: 11px; opacity: .6; }
.file-row      { font-family: monospace; font-size: 12px; }
.sidebar           { background: alpha(@card_bg_color, .5); }
.sidebar-row-title { font-size: 13px; font-weight: 600; }
.sidebar-row-time  { font-size: 11px; opacity: .55; }
.input-frame       { background: alpha(@card_fg_color, .05);
                      border: 1px solid alpha(@borders, .9);
                      border-radius: 12px; padding: 2px 8px; }
.input-frame:focus-within { border-color: @accent_bg_color; }
.mode-toggle       { font-size: 12px; }
.mode-toggle-auto   { color: @warning_color; }
"""


# Core (config + Brain + skills + памет) и run_tool_loop (пълния агентен
# цикъл) са в genesis_agent.agent_core — споделени с Jarvis гласовия фронтенд,
# за да не се дублира логика между фронтендите (виж модула за детайли).


# ─────────────────────────────────────────────────────────────────────────────
# Съобщения в чата
# ─────────────────────────────────────────────────────────────────────────────
def _label(text: str, *, selectable: bool = True, css: str | None = None,
           wrap: bool = True) -> Gtk.Label:
    lb = Gtk.Label(label=text, xalign=0.0, wrap=wrap, selectable=selectable)
    if wrap:
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

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.body.add_css_class(f"msg-{kind}")
        self.append(self.body)
        self.set_text(text)

    def set_text(self, text: str) -> None:
        """Пренарежда тялото за нов текст — извикано наведнъж (стария път) или
        многократно с растящ префикс (typewriter reveal, виж `_animate_reveal`).
        Пресъздава децата всеки път — съобщенията са кратки, евтино е, и
        избягва счупено state при code-block split-а на непълен текст."""
        child = self.body.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.body.remove(child)
            child = nxt
        for is_code, chunk in self._split_code(text):
            if not chunk.strip():
                continue
            if is_code:
                sw = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER)
                sw.set_child(_label(chunk.rstrip()))
                sw.add_css_class("code-block")
                self.body.append(sw)
            else:
                self.body.append(_label(chunk.strip()))

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


def _animate_reveal(widget: MessageWidget, full_text: str, on_tick=None) -> None:
    """Typewriter reveal (design note, 2026-07-28) — Brain-ът не прави реален
    token-streaming (HTTP слоят не е SSE, пренаписването е отделен, по-голям
    проект), но пълният отговор дошъл наведнъж все пак може да се РАЗКРИВА
    прогресивно — затваря по-голямата част от усещането за "работи в реално
    време" на много по-малка цена. Chunk-размерът е адаптивен (~80 стъпки
    общо), за да не отнема нито 3 знака 500мс на кратко "OK", нито 8000
    символа цяла вечност на дълъг отговор."""
    if not full_text:
        return
    chunk = max(8, len(full_text) // 80)
    state = {"i": 0}

    def step() -> bool:
        state["i"] += chunk
        widget.set_text(full_text[: state["i"]])
        if on_tick:
            on_tick()
        return state["i"] < len(full_text)

    GLib.timeout_add(20, step)


class ToolWidget(Gtk.Box):
    """Резултат от инструмент — сгънат по подразбиране, за да не залива чата.

    За WRITE_FILE с diff (виж agent_core.run_tool_loop's `extra`) показва
    оцветен unified diff вместо голия "✓ записани N символа" статус — вижда
    се какво реално се промени във файла, не само че нещо е записано."""

    def __init__(self, name: str, result: str, diff: str | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        first = (result.strip().split("\n") or [""])[0][:80]
        expanded_default = bool(diff)  # diff-ове са полезни разгънати веднага
        exp = Gtk.Expander(label=f"🔧 {name} — {first}", expanded=expanded_default)
        if diff:
            sw = Gtk.ScrolledWindow(max_content_height=400, propagate_natural_height=True)
            sw.set_child(self._diff_view(diff))
            sw.add_css_class("code-block")
            exp.set_child(sw)
        else:
            sw = Gtk.ScrolledWindow(max_content_height=280, propagate_natural_height=True)
            lb = _label(result[:8000])
            lb.add_css_class("tool-out")
            sw.set_child(lb)
            sw.add_css_class("code-block")
            exp.set_child(sw)
        self.append(exp)

    @staticmethod
    def _diff_view(diff: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        for line in diff.splitlines():
            css = "tool-out"
            if line.startswith("+") and not line.startswith("+++"):
                css = "diff-add"
            elif line.startswith("-") and not line.startswith("---"):
                css = "diff-del"
            elif line.startswith("@@"):
                css = "diff-hunk"
            box.append(_label(line, css=css))
        return box


# ─────────────────────────────────────────────────────────────────────────────
# Смяна на модел
# ─────────────────────────────────────────────────────────────────────────────
class ModelPicker(Gtk.MenuButton):
    """Списък от достъпните модели (Core.available_models()) в popover — избор
    заковава Core.pin_model за следващите обаждания. "Автоматично" връща
    pin_model на None (Brain-ът пак решава сам, top-down верига)."""

    def __init__(self, core: Core, on_pick) -> None:
        super().__init__(icon_name="applications-engineering-symbolic")
        self.set_tooltip_text("Смяна на модел")
        self.core = core
        self.on_pick = on_pick
        self.popover = Gtk.Popover()
        self.set_popover(self.popover)
        self.refresh()

    def refresh(self) -> None:
        # width_request фиксира popover-а на удобна ширина независимо от
        # най-краткия ред — wrap=False пази всеки ред на един ред (design
        # note 2026-07-29: METKO докладва прекалено тясно/нечетимо меню —
        # wrapping label-ите смаляваха natural width заявката до минимума).
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_size_request(360, -1)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.append(_label("Модел", selectable=False, css="speaker", wrap=False))

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")

        # selectable=False е критично тук, не козметика: selectable label в
        # GTK4 прихваща клика за текст-селекция и НЕ го пуска да стигне до
        # ListBoxRow-а → row-activated никога не гърми, изборът изглежда
        # мъртъв (METKO докладва точно това, 2026-07-29). Предшестващ бъг —
        # _label()'s default (selectable=True) никога не е бил override-нат
        # тук, само в по-новия Sidebar.
        auto_row = Gtk.ListBoxRow()
        auto_row.set_child(_label("🔀 Автоматично (най-добър наличен)",
                                   selectable=False, wrap=False))
        auto_row.genesis_model = None
        listbox.append(auto_row)

        try:
            models = self.core.available_models()
        except Exception:
            models = []
        for m in models:
            row = Gtk.ListBoxRow()
            size = m.get("size_b") or 0
            suffix = f" · {int(size)}B" if size else ""
            row.set_child(_label(f"{m['model']}  ·  {m['provider']}{suffix}",
                                  selectable=False, wrap=False))
            row.genesis_model = m["provider"], m["model"]
            listbox.append(row)

        def on_row_activated(_lb: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
            picked = getattr(row, "genesis_model", None)
            self.core.pin_model = picked
            self.on_pick(picked)
            self.popover.popdown()

        listbox.connect("row-activated", on_row_activated)
        sw = Gtk.ScrolledWindow(max_content_height=360, propagate_natural_height=True,
                                 propagate_natural_width=True)
        sw.set_child(listbox)
        box.append(sw)
        self.popover.set_child(box)


# ─────────────────────────────────────────────────────────────────────────────
# Auto-approve / Ръчно — реален sandbox режим
# ─────────────────────────────────────────────────────────────────────────────
class ModeToggle(Gtk.ToggleButton):
    """Превключва genesis_agent.sandbox.SandboxPolicy.mode на живо (design
    note, 2026-07-29 — METKO поиска auto-approve/manual контрол до входа,
    като референтния Claude Code Desktop "Auto"). Реален механизъм, не
    декоративен бутон: "Ръчно" = mode="interactive" (истински модален
    диалог на всяка CONFIRM операция, СЪЩОТО поведение като по подразбиране
    досега); "Автоматично" = mode="allow" (CONFIRM операции минават без
    питане — истински риск, затова изключено по подразбиране и оцветено
    предупредително, когато е активно). BLOCKED операциите не зависят от
    режима изобщо (винаги отказани), тук не се пипа."""

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("mode-toggle")
        self.set_tooltip_text(
            "Ръчно: всяка рискова операция чака твое потвърждение.\n"
            "Автоматично: рисковите операции минават БЕЗ питане — внимавай."
        )
        self.connect("toggled", lambda *_: self._sync())
        self._sync()

    def _sync(self) -> None:
        auto = self.get_active()
        self.set_label("🔓 Автоматично" if auto else "🔒 Ръчно")
        if auto:
            self.add_css_class("mode-toggle-auto")
        else:
            self.remove_css_class("mode-toggle-auto")
        try:
            from genesis_agent import sandbox
            sandbox.set_policy(sandbox.SandboxPolicy(
                mode="allow" if auto else "interactive", confirm_fn=_gui_confirm
            ))
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Работна директория — файлов панел
# ─────────────────────────────────────────────────────────────────────────────
_IGNORED_DIRS = {".git", "__pycache__", ".mypy_cache", "node_modules", ".venv",
                  "venv", ".sandbox_run", "*.egg-info", "build", "dist"}
_MAX_FILES_LISTED = 400


def _list_workspace_files(root: Path) -> list[Path]:
    """Плосък, сортиран списък на файловете в workspace-а (не дърво — по-прост
    widget, достатъчен за "виждам с какво работи агентът"). Ограничен
    (_MAX_FILES_LISTED), за да не увисне UI-то на огромен repo."""
    out: list[Path] = []
    try:
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _IGNORED_DIRS or part.endswith(".egg-info") for part in p.parts):
                continue
            out.append(p)
            if len(out) >= _MAX_FILES_LISTED:
                break
    except OSError:
        pass
    return out


class WorkspacePanel(Gtk.Box):
    """Свит по подразбиране страничен панел — реалният код-контекст на
    разговора (design note, 2026-07-28): без него приложението е чист чат,
    нищо на екрана не показва, че агентът гледа файловете на потребителя.
    Read-only (клик → преглед); писането минава изцяло през WRITE_FILE/
    sandbox, панелът не е редактор."""

    def __init__(self, workspace: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.set_size_request(260, -1)
        self.workspace = Path(workspace)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header.set_margin_start(8)
        header.set_margin_end(8)
        header.set_margin_top(8)
        header.append(_label(f"📁 {self.workspace.name}", selectable=False, css="speaker"))
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"])
        refresh_btn.set_valign(Gtk.Align.CENTER)
        refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.append(refresh_btn)
        self.append(header)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        list_sw = Gtk.ScrolledWindow(vexpand=True)
        list_sw.set_child(self.listbox)
        self.append(list_sw)

        self.preview = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.NONE,
                                     monospace=True)
        preview_sw = Gtk.ScrolledWindow(min_content_height=200, vexpand=False)
        preview_sw.set_child(self.preview)
        preview_sw.add_css_class("code-block")
        self.append(preview_sw)

        self.listbox.connect("row-activated", self._on_row_activated)
        self.refresh()

    def refresh(self) -> None:
        child = self.listbox.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt
        files = _list_workspace_files(self.workspace)
        for f in files:
            row = Gtk.ListBoxRow()
            rel = f.relative_to(self.workspace)
            row.set_child(_label(str(rel), css="file-row"))
            row.genesis_path = f
            self.listbox.append(row)
        if not files:
            row = Gtk.ListBoxRow(selectable=False, activatable=False)
            row.set_child(_label("(празно или недостъпно)", css="status-dim"))
            self.listbox.append(row)

    def _on_row_activated(self, _lb: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        path = getattr(row, "genesis_path", None)
        if path is None:
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) > 20000:
                text = text[:20000] + "\n… [отрязано]"
        except OSError as e:
            text = f"[не може да се прочете: {e}]"
        self.preview.get_buffer().set_text(text)


# ─────────────────────────────────────────────────────────────────────────────
# Странична лента — Recents (Claude Code Desktop reference, design note 2026-07-29)
# ─────────────────────────────────────────────────────────────────────────────
def _relative_time(iso: str) -> str:
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - ts).total_seconds()
    if secs < 60:
        return "току-що"
    if secs < 3600:
        return f"преди {int(secs // 60)} мин"
    if secs < 86400:
        return f"преди {int(secs // 3600)} ч"
    days = int(secs // 86400)
    if days == 1:
        return "вчера"
    if days < 7:
        return f"преди {days} дни"
    return ts.strftime("%d.%m.%Y")


class Sidebar(Gtk.Box):
    """Списък от предишни разговори (gui_sessions.py) — нов бутон, търсене по
    заглавие, клик отваря стария разговор. Преди тази промяна приложението
    беше единичен прозорец без начин да се преотвори стар разговор; тук няма
    собствено състояние на текущата сесия — само callback-и, ЕДИНСТВЕНИЯТ
    "истина" източник за текущия разговор си остава Window.messages."""

    def __init__(self, on_new, on_select) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.on_select = on_select
        self.add_css_class("sidebar")
        self.set_size_request(240, -1)

        new_btn = Gtk.Button(label="＋ Нов разговор")
        new_btn.add_css_class("suggested-action")
        new_btn.set_margin_top(8)
        new_btn.set_margin_start(8)
        new_btn.set_margin_end(8)
        new_btn.connect("clicked", lambda *_: on_new())
        self.append(new_btn)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.set_filter_func(self._filter_row)
        self.listbox.connect("row-activated", self._on_row_activated)

        self.search = Gtk.SearchEntry(placeholder_text="Търсене…")
        self.search.set_margin_start(8)
        self.search.set_margin_end(8)
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
        self.append(self.search)

        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(self.listbox)
        self.append(sw)

        self.refresh()

    def refresh(self) -> None:
        child = self.listbox.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt
        try:
            sessions = gui_sessions.list_recent()
        except Exception:
            sessions = []
        for s in sessions:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(10)
            box.set_margin_end(10)
            box.append(_label(s["title"], selectable=False, css="sidebar-row-title"))
            box.append(_label(_relative_time(s["updated_at"]), selectable=False,
                               css="sidebar-row-time"))
            row.set_child(box)
            row.genesis_session_id = s["id"]
            row.genesis_title = s["title"].lower()
            self.listbox.append(row)

    def _filter_row(self, row: Gtk.ListBoxRow) -> bool:
        query = self.search.get_text().strip().lower()
        if not query:
            return True
        return query in getattr(row, "genesis_title", "")

    def _on_row_activated(self, _lb: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        sid = getattr(row, "genesis_session_id", None)
        if sid:
            self.on_select(sid)


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
        self.session_id = gui_sessions.new_id()

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.subtitle = Gtk.Label(label="готов", css_classes=["status-dim"])
        title = Adw.WindowTitle(title="Genesis Agent", subtitle=self._idle_subtitle())
        self.title_widget = title
        header.set_title_widget(title)

        sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", active=True)
        sidebar_btn.set_tooltip_text("Разговори")
        header.pack_start(sidebar_btn)

        # Studio-ерата model picker вече живее в долната лента (виж bottom по-долу),
        # не в header-а — Claude Code Desktop reference го показва до входа.
        self.model_picker = ModelPicker(core, self.on_model_picked)

        workspace_btn = Gtk.ToggleButton(icon_name="folder-symbolic")
        workspace_btn.set_tooltip_text("Работна директория")
        workspace_btn.connect("toggled", self.on_toggle_workspace)
        self.workspace_btn = workspace_btn
        header.pack_end(workspace_btn)

        menu = Gio.Menu()
        menu.append("Нов разговор", "win.clear")
        menu.append("Задачи и решения", "win.tasks")
        menu.append("Разход на токени", "win.budget")
        menu.append("Команди (/help)", "win.help")
        btn_menu = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(btn_menu)
        toolbar.add_top_bar(header)

        for name, cb in (
            ("clear", self.on_clear),
            ("tasks", self.on_tasks),
            ("budget", self.on_budget),
            ("help", self.on_help),
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
        entry_sw.add_css_class("input-frame")

        self.send_btn = Gtk.Button(icon_name="document-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.connect("clicked", lambda *_: self.submit())

        self.spinner = Gtk.Spinner()
        self.spinner.set_valign(Gtk.Align.END)

        self.mode_toggle = ModeToggle()
        self.mode_toggle.set_valign(Gtk.Align.END)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.set_margin_start(10)
        bottom.set_margin_end(10)
        bottom.set_margin_top(4)
        bottom.set_margin_bottom(10)
        bottom.append(self.mode_toggle)
        bottom.append(entry_sw)
        entry_sw.set_hexpand(True)
        bottom.append(self.model_picker)
        bottom.append(self.spinner)
        bottom.append(self.send_btn)

        chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        chat_box.append(self.scroll)
        chat_box.append(bottom)

        self.workspace_panel = WorkspacePanel(core.workspace) if core.ok else None
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_start_child(chat_box)
        self.paned.set_resize_start_child(True)
        self.paned.set_shrink_start_child(False)
        if self.workspace_panel is not None:
            self.paned.set_end_child(self.workspace_panel)
            self.paned.set_resize_end_child(False)
            # Свит по подразбиране (визуален шум за някой, който само чати) —
            # showing/hiding end_child е по-надежден в GTK4 от position-хакове.
            self.workspace_panel.set_visible(False)
        toolbar.set_content(self.paned)

        self.sidebar = Sidebar(self.on_clear, self.load_session)
        self.split_view = Adw.OverlaySplitView()
        self.split_view.set_sidebar(self.sidebar)
        self.split_view.set_content(toolbar)
        sidebar_btn.bind_property(
            "active", self.split_view, "show-sidebar",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        self.set_content(self.split_view)

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

    def _idle_subtitle(self) -> str:
        """Заглавието винаги показва активния модел (не само "готов"/"мисли…")
        — точно липсата на тази видимост беше разликата с терминала, който
        винаги знае/показва какво отговаря (design note, 2026-07-28)."""
        pin = getattr(self.core, "pin_model", None)
        return f"модел: {pin[1]}" if pin else "готов · авто модел"

    def on_key(self, _ctrl, keyval, _code, state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                return False
            self.submit()
            return True
        return False

    # ── Модел + работна директория ──────────────────────────────────────────
    def on_model_picked(self, picked: tuple[str, str] | None) -> None:
        self.set_status(self._idle_subtitle())
        label = picked[1] if picked else "автоматично"
        self.add_widget(MessageWidget("⚙️ Модел", f"Превключено на: {label}", kind="assistant"))

    def on_toggle_workspace(self, btn: Gtk.ToggleButton) -> None:
        if self.workspace_panel is None:
            return
        self.workspace_panel.set_visible(btn.get_active())
        if btn.get_active():
            self.workspace_panel.refresh()

    def on_help(self, *_a) -> None:
        self.add_widget(MessageWidget("💡 Команди", _HELP_TEXT, kind="assistant"))

    # ── Действия от менюто ───────────────────────────────────────────────────
    def on_clear(self, *_a) -> None:
        # Споделено между менюто "Нов разговор" и sidebar-ния "＋" бутон —
        # старият разговор се пази (save-then-reset), не се губи мълчаливо.
        self._save_current_session()
        self.session_id = gui_sessions.new_id()
        self.messages = deque(
            [{"role": "system", "content": self.core.system_prompt}], maxlen=30
        )
        self._rebuild_chat_widgets([])
        self._refresh_sidebar()

    def load_session(self, session_id: str) -> None:
        if self.busy:
            return
        self._save_current_session()
        msgs = gui_sessions.load(session_id)
        if msgs is None:
            return
        self.session_id = session_id
        self.messages = deque(
            [{"role": "system", "content": self.core.system_prompt}] + msgs, maxlen=30
        )
        self._rebuild_chat_widgets(msgs)

    def _rebuild_chat_widgets(self, msgs: list[dict]) -> None:
        """Пресъздава видимите балончета от съхранена история. Ролите tool/
        system умишлено не се пресъздават визуално (ToolWidget-ите изискват
        оригиналния diff, който не пазим отделно) — пълният контекст пак е в
        self.messages за модела, само видимата стара тул-стъпка липсва."""
        child = self.chat.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.chat.remove(child)
            child = nxt
        shown = False
        for m in msgs:
            role = m.get("role")
            content = m.get("content")
            if not isinstance(content, str) or not content:
                continue
            if role == "user":
                self.add_widget(MessageWidget("Ти", content, kind="user"))
                shown = True
            elif role == "assistant":
                self.add_widget(MessageWidget("Genesis", content, kind="assistant"))
                shown = True
        if not shown:
            self.add_widget(MessageWidget("Genesis", "Нов разговор.", kind="assistant"))

    def _save_current_session(self) -> None:
        try:
            gui_sessions.save(self.session_id, list(self.messages))
        except Exception:
            pass

    def _refresh_sidebar(self) -> None:
        try:
            self.sidebar.refresh()
        except Exception:
            pass

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
        self._save_current_session()
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
        if text.startswith("/") and self._handle_slash_command(text):
            return
        self.add_widget(MessageWidget("Ти", text, kind="user"))
        self.messages.append({"role": "user", "content": text})
        self.core.remember("user", text)
        self._start_work()

    def _handle_slash_command(self, text: str) -> bool:
        """Команди, познати от терминалния фронтенд, сега и в GUI входа —
        не всеки клиент чете менюто, но всеки пробва да пише /команда first
        (design note, 2026-07-28, "Claude Code parity" заявка). Връща True
        ако командата е разпозната (и обработена) — иначе текстът минава
        нормално към LLM-а (напр. код-коментар, започващ с "/", не команда)."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/model":
            self.model_picker.popover.popup()
            return True
        if cmd == "/clear":
            self.on_clear()
            return True
        if cmd == "/tasks":
            self.on_tasks()
            return True
        if cmd == "/budget":
            self.on_budget()
            return True
        if cmd == "/help":
            self.on_help()
            return True
        if cmd in ("/done", "/drop"):
            if not arg:
                self.add_widget(MessageWidget(
                    "⚠️", f"Употреба: {cmd} <id> (виж /tasks за id-та)", kind="error"))
                return True
            if not self.core.wm:
                self.add_widget(MessageWidget("⚠️", "Няма памет на работата.", kind="error"))
                return True
            try:
                out = self.core.wm.close_thread(arg, drop=(cmd == "/drop"))
            except Exception as e:
                out = f"Грешка: {e}"
            self.add_widget(MessageWidget("📋", out, kind="assistant"))
            return True
        return False

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
            def add() -> None:
                w = MessageWidget("Genesis", "", kind="assistant")
                self.add_widget(w)
                _animate_reveal(w, text, on_tick=self._scroll_bottom)
            GLib.idle_add(add)

        def on_tool_result(name: str, result: str, diff: str | None = None) -> None:
            GLib.idle_add(self.add_widget, ToolWidget(name, result, diff))
            if diff and self.workspace_panel is not None and self.workspace_panel.get_visible():
                GLib.idle_add(self.workspace_panel.refresh)

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
        self.set_status(self._idle_subtitle())
        self._save_current_session()
        self._refresh_sidebar()
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
        # Sandbox политиката вече се задава от ModeToggle (в Window.__init__,
        # с mode="interactive" по подразбиране — идентично на предишното тук)
        # — един източник на истина вместо да се дублира на две места.
        win = self.props.active_window or Window(self, self.core)
        _WINDOW = win
        win.present()


def main() -> int:
    core = Core()
    return App(core).run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
