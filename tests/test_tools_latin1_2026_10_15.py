"""Latin-1 / cp1252 файлове през READ / EDIT / WRITE / SEARCH (2026-10-15).

Преди: „Café crème“ в cp1252 се виждаше като „Caf� cr�me“, ред с ударение не
можеше да се редактира (anchor-ът от READ_FILE носи U+FFFD, файлът — байта),
нова буква влизаше като UTF-8 в иначе cp1252 файл (два кодирания в един файл),
а READ→WRITE се отказваше. Разпознаването не бива да взима cp1251 (български)
или UTF-8 с един чужд байт за cp1252: тогава се показва mojibake, а запис с
UTF-8 резерва го закова на диска. Пуснато срещу стария код: 25 падаха, а
минаваха само пазачите — `test_cp1251_*`, `test_*_stays_utf8_*`,
`test_cp1252_write_preview_*` и
`test_read_file_names_the_encoding_that_write_will_use` (той пада с новото
разпознаване без поправката в READ_FILE).
"""
from __future__ import annotations

import pytest

import genesis_skills as gs
from genesis_agent import code_edit, mentions


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    gs.set_workspace(tmp_path)
    gs._SEEN_PATHS.clear()
    monkeypatch.setattr("genesis_agent.sandbox._POLICY", gs.sandbox.SandboxPolicy())
    yield tmp_path
    gs.set_workspace(gs._PROJECT_ROOT)


def _body(out: str) -> str:
    return out.split("\n", 1)[1]


_FR = "# Café crème, naïve Müller — 5 €\r\nNAME = 'Café'\r\nX = 1\r\n"


# ── разпознаване ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Café", "naïve", "Müller", "Straße", "año", "São Paulo", "città", "København",
    "x = 'Café'\n", "¿Qué tal?", "l’été", "col·lecció",
    "Die Größe der Äpfel ist schön, aber die Bäume müssen geschnitten werden.",
    "Ça va très bien, merci. Nous sommes arrivés hier soir à l’hôtel.",
    "El niño comió jamón y después salió a jugar al jardín con su hermana.",
    "Þórður Guðjónsson býr í Reykjavík.",
    "The café’s naïve résumé — “quoted” — costs €5.",
    _FR,
])
def test_western_text_in_cp1252_is_cp1252(text):
    raw = text.encode("cp1252")
    decoded, enc = code_edit.decode_text(raw)
    assert (decoded, enc) == (text, "cp1252")
    assert code_edit.encode_text(decoded, enc) == raw


@pytest.mark.parametrize("text", [
    "Да", "ОК", "Ж", "и в с а", "x = 'Да'\ny = 'Не'\n", "# ИД\n", "№ 5 от 12.03.2024",
    "Здравей, свят!", "# Грешка при запис\n", "Цена: 12 лв.", "яко",
    "name,city\r\nИван,София\r\nМария,Пловдив\r\nGeorge,Varna\r\n",
    "Файлът е записан успешно в папката „Документи“.",
    "Ще се видим в 5 ч. — до скоро!",
    "Привет, как дела? Всё хорошо, спасибо.",
    "Україна — це країна в Європі; її столиця — Київ.",
    "Србија и Македонија: љубав, њива, ђак, ћерка, џеп.",
])
def test_cp1251_text_is_never_taken_for_cp1252(text):
    raw = text.encode("cp1251")
    decoded, enc = code_edit.decode_text(raw)
    assert enc != "cp1252", (text, decoded)
    assert code_edit.encode_text(decoded, enc) == raw


@pytest.mark.parametrize("raw", [
    b"Caf\xc3\xa9 and Caf\xe9",                       # UTF-8 „é“ и latin-1 „é“
    b"M\xc3\xbcller M\xfcller M\xfcller M\xfcller",   # „Ã¼“ би бил mojibake
    b"\xe2\x80\x99 smart and Caf\xe9",                # UTF-8 „’“ = „â€™“ в cp1252
    "Грешка: файлът не е намерен\n".encode() + b"# Caf\xe9\n",
    b"x = '\xe2\x86\x92'\ny = 'na\xefve'\n",
    b"# (c) 2001 \xa9 Acme\n",                        # само знак, никоя буква
    b"Preis: 10 \x80\n",
    b"\xe9\n",                                        # една буква сама: „и“ или „é“?
    b"Caf\xe9 \x81\n",                                # байт, който cp1252 няма
])
def test_unsure_input_stays_utf8_with_raw_bytes(raw):
    decoded, enc = code_edit.decode_text(raw)
    assert enc == "utf-8", (raw, decoded)
    assert code_edit.encode_text(decoded, enc) == raw


@pytest.mark.parametrize("text", [
    # cp1250 (чешки, полски, унгарски, румънски) прочетен като cp1252 е
    # „Pøíliš“, „Dzieñ … siê“, „Árvíztûrõ“, „ºcoalã“ — по-добре U+FFFD.
    "Příliš žluťoučký kůň úpěl ďábelské ódy.",
    "Dobrý den, jak se máte? Dnes je krásné počasí a půjdeme na procházku.",
    "Česká republika",
    "Zażółć gęślą jaźń", "Łódź", "książka",
    "Dzień dobry, jak się masz? Dziś jest piękna pogoda.",
    "Árvíztűrő tükörfúrógép",
    "Fişierul nu a fost găsit; verificaţi calea şi încercaţi din nou.",
])
def test_central_european_cp1250_stays_utf8_with_raw_bytes(text):
    raw = text.encode("cp1250")
    decoded, enc = code_edit.decode_text(raw)
    assert enc == "utf-8", (text, decoded)
    assert code_edit.encode_text(decoded, enc) == raw


# ── READ / EDIT / WRITE ──────────────────────────────────────────────────────

def test_cp1252_read_shows_the_letters_and_names_the_encoding(ws):
    (ws / "m.py").write_bytes(_FR.encode("cp1252"))
    out = gs._tool_read_file("m.py")
    assert "cp1252" in out.splitlines()[0]
    assert _body(out) == _FR.replace("\r\n", "\n")


def test_cp1252_edit_of_an_accented_line_keeps_every_other_byte(ws):
    p = ws / "m.py"
    raw = _FR.encode("cp1252")
    p.write_bytes(raw)
    out = gs.dispatch_tool_call("EDIT_FILE", {"path": "m.py", "old": "NAME = 'Café'",
                                              "new": "NAME = 'Café au lait'"})
    assert "✓" in out and "cp1252" in out, out
    assert p.read_bytes() == raw.replace(b"NAME = 'Caf\xe9'", b"NAME = 'Caf\xe9 au lait'")


def test_cp1252_edit_writes_new_accents_in_the_file_encoding(ws):
    # Преди: „déjà“ влизаше като UTF-8 (C3 A9) до latin-1 байтовете — файл с
    # две кодирания, който никой редактор не отваря правилно.
    p = ws / "m.py"
    raw = _FR.encode("cp1252")
    p.write_bytes(raw)
    out = gs.dispatch_tool_call("EDIT_FILE", {"path": "m.py", "old": "X = 1", "new": "X = 'déjà'"})
    assert "✓" in out, out
    assert p.read_bytes() == raw.replace(b"X = 1", b"X = 'd\xe9j\xe0'")


def test_cp1252_edit_with_a_char_outside_cp1252_is_refused(ws):
    p = ws / "m.py"
    raw = _FR.encode("cp1252")
    p.write_bytes(raw)
    out = gs.dispatch_tool_call("EDIT_FILE", {"path": "m.py", "old": "X = 1", "new": "X = 'Здравей'"})
    assert "cp1252" in out and "НЕ Е ПРОМЕНЕН" in out, out
    assert p.read_bytes() == raw


def test_cp1252_read_then_write_roundtrip_keeps_the_bytes(ws):
    p = ws / "m.py"
    raw = _FR.encode("cp1252")
    p.write_bytes(raw)
    body = _body(gs._tool_read_file("m.py"))
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "m.py",
                                               "content": body.replace("X = 1", "X = 2")})
    assert "✓" in out, out
    assert p.read_bytes() == raw.replace(b"X = 1", b"X = 2")


def test_cp1252_write_with_char_outside_cp1252_becomes_correct_utf8(ws):
    # UTF-8 резервата на WRITE_FILE записва ТОВА, което моделът е видял: при
    # правилно разпознаване това е „Café“, не „CafÃ©“.
    p = ws / "m.py"
    p.write_bytes(_FR.encode("cp1252"))
    body = _body(gs._tool_read_file("m.py"))
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "m.py",
                                               "content": body + "# Здравей\n"})
    assert "UTF-8" in out, out
    assert p.read_bytes() == (_FR + "# Здравей\r\n").encode("utf-8")


def test_cp1252_write_preview_shows_only_the_changed_line(ws):
    from genesis_agent import agent_core
    (ws / "m.py").write_bytes(_FR.encode("cp1252"))
    body = _body(gs._tool_read_file("m.py"))
    diff = agent_core._diff_for_write(gs, {"path": "m.py", "content": body.replace("X = 1", "X = 2")})
    assert diff is not None
    changed = [ln for ln in diff.splitlines()
               if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    assert changed == ["-X = 1", "+X = 2"], diff


def test_cp1251_crlf_edit_still_keeps_the_encoding(ws):
    p = ws / "legacy.txt"
    raw = "Здравей, свят\r\nДа\r\nname = 1\r\n".encode("cp1251")
    p.write_bytes(raw)
    out = gs.dispatch_tool_call("EDIT_FILE", {"path": "legacy.txt", "old": "name = 1", "new": "name = 2"})
    assert "cp1251" in out, out
    assert p.read_bytes() == raw.replace(b"name = 1", b"name = 2")


def test_read_file_names_the_encoding_that_write_will_use(ws):
    # Началото (32 KB) е само latin-1, по-нататък UTF-8 кирилица: целият файл
    # е UTF-8 с чужди байтове. READ_FILE, решил по началото „cp1252“, показва
    # „Café“; WRITE_FILE (решава по 2 MB) после пише „é“ като C3 A9 — байтовете
    # на всеки ред с ударение се сменят. Един изглед и за двата.
    p = ws / "late.txt"
    raw = b"Caf\xe9 au lait\n" * 3000 + "Здравей, свят — това е UTF-8\n".encode() * 1000
    p.write_bytes(raw)
    assert code_edit.decode_text(raw)[1] == "utf-8"
    out = gs._tool_read_file("late.txt")
    assert "cp1252" not in out.splitlines()[0], out.splitlines()[0]


# ── @файл ─────────────────────────────────────────────────────────────────

def test_a_mention_reads_cp1252_like_read_file(ws):
    (ws / "m.py").write_bytes(_FR.encode("cp1252"))
    text, _notes = mentions.expand("виж @m.py", ws)
    assert "NAME = 'Café'\n" in text and "�" not in text
