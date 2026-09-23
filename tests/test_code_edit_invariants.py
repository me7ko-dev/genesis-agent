"""Инварианти на genesis_agent.code_edit — редакторът, който пипа чужди файлове.

Съществуващите тестове покриват щастливите пътища и основните откази. Тук са
свойствата, които трябва да държат за ВСЕКИ вход, защото провалът им е тих:
редакция, която "почти" е сработила, оставя повреден файл, тестовете падат
някъде другаде, а diff-ът не се чете. Точно сценарият, заради който модулът
съществува (виж docstring-а му за WRITE_FILE върху чужд модул от 2000 реда).

Генераторите са детерминистични (фиксиран seed) — същата полза като
hypothesis, без нова зависимост в CI, и провалът се възпроизвежда точно.

Най-важният инвариант е първи: ОТКАЗЪТ НЕ ПИПА ДИСКА. Модулът обещава в
docstring-а си, че всяко отказано редактиране оставя файла точно както е бил;
ако това не е вярно, всичко останало в него е без значение.
"""
from __future__ import annotations

import random
import string

import pytest

from genesis_agent.code_edit import edit_file

SEED = 20260920

_SAMPLE = (
    "from __future__ import annotations\n"
    "\n"
    "def parse(s):\n"
    "    value = 1\n"
    "    return value\n"
    "\n"
    "def render(s):\n"
    "    value = 1\n"
    "    return value\n"
)


@pytest.fixture()
def sample(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text(_SAMPLE, encoding="utf-8")
    return p


class TestARefusalNeverTouchesTheFile:
    """Всеки отказ трябва да е пълен: или цялата редакция, или нищо."""

    def test_missing_anchor_leaves_the_file_byte_identical(self, sample) -> None:
        before = sample.read_bytes()
        res = edit_file(sample, old="напълно липсващ текст", new="каквото и да е")
        assert not res.ok
        assert sample.read_bytes() == before

    def test_ambiguous_anchor_leaves_the_file_byte_identical(self, sample) -> None:
        """`    value = 1\\n` е на два реда — редактирането на "първия" е
        точно тихата повреда, която модулът отказва да направи."""
        before = sample.read_bytes()
        res = edit_file(sample, old="    value = 1\n", new="    value = 2\n")
        assert not res.ok
        assert sample.read_bytes() == before

    def test_empty_anchor_is_refused_without_writing(self, sample) -> None:
        before = sample.read_bytes()
        res = edit_file(sample, old="", new="нещо")
        assert not res.ok
        assert sample.read_bytes() == before

    def test_a_missing_file_is_refused_and_not_created(self, tmp_path) -> None:
        target = tmp_path / "nope.py"
        res = edit_file(target, old="x", new="y")
        assert not res.ok
        assert not target.exists(), "отказът не бива да създава файла"

    def test_random_junk_anchors_never_corrupt_the_file(self, sample) -> None:
        rnd = random.Random(SEED)
        before = sample.read_bytes()
        for _ in range(500):
            old = "".join(rnd.choice(string.printable)
                          for _ in range(rnd.randint(0, 40)))
            new = "".join(rnd.choice(string.printable)
                         for _ in range(rnd.randint(0, 40)))
            res = edit_file(sample, old=old, new=new)
            if not res.ok:
                assert sample.read_bytes() == before, (
                    f"отказан edit е пипнал файла (seed={SEED}, old={old!r})")
            else:
                # Случайният низ наистина е съвпаднал — връщаме изходното
                # състояние, за да остане цикълът сравним.
                sample.write_bytes(before)


class TestASuccessChangesOnlyWhatWasNamed:
    def test_the_rest_of_the_file_is_untouched(self, sample) -> None:
        res = edit_file(sample, old="def parse(s):", new="def parse(s: str) -> dict:")
        assert res.ok, res.detail
        after = sample.read_text(encoding="utf-8")
        assert "def parse(s: str) -> dict:" in after
        # Всичко останало, ред по ред, е същото.
        for line in _SAMPLE.splitlines():
            if line != "def parse(s):":
                assert line in after, f"загубен ред: {line!r}"

    def test_replace_all_reports_how_many_it_touched(self, sample) -> None:
        res = edit_file(sample, old="    value = 1\n", new="    value = 2\n",
                        replace_all=True)
        assert res.ok, res.detail
        assert res.replacements == 2, res.replacements
        assert sample.read_text(encoding="utf-8").count("value = 2") == 2

    def test_a_no_op_edit_does_not_claim_changes(self, sample) -> None:
        """old == new не е промяна; ако се отчете като такава, агентът ще
        реши, че е свършил нещо, и ще спре да търси истинската причина."""
        res = edit_file(sample, old="def parse(s):", new="def parse(s):")
        assert sample.read_text(encoding="utf-8") == _SAMPLE
        if res.ok:
            assert res.lines_changed == (0, 0), res.lines_changed


class TestEncodingAndLineEndings:
    """Проектът поддържа Windows и държи .gitattributes точно заради тези
    два капана. Редактор, който мълчаливо нормализира, поврежда всеки файл,
    който докосне на такава машина."""

    def test_crlf_line_endings_survive_an_edit(self, tmp_path) -> None:
        p = tmp_path / "win.py"
        p.write_bytes(b"alpha = 1\r\nbeta = 2\r\ngamma = 3\r\n")
        res = edit_file(p, old="beta = 2", new="beta = 22")
        assert res.ok, res.detail
        raw = p.read_bytes()
        assert b"beta = 22" in raw
        assert raw.count(b"\r\n") == 3, "CRLF краищата не бива да се сменят тихо"
        assert b"\n\n" not in raw.replace(b"\r\n", b"")

    def test_unicode_content_is_preserved(self, tmp_path) -> None:
        p = tmp_path / "bg.py"
        p.write_text("# Здравей, свят\nx = 'мир'\ny = '日本語'\n", encoding="utf-8")
        res = edit_file(p, old="x = 'мир'", new="x = 'мир и любов'")
        assert res.ok, res.detail
        after = p.read_text(encoding="utf-8")
        assert "мир и любов" in after
        assert "日本語" in after, "несвързан unicode не бива да се губи"
        assert "# Здравей, свят" in after

    def test_a_unicode_anchor_matches(self, tmp_path) -> None:
        p = tmp_path / "bg2.py"
        p.write_text("съобщение = 'старо'\n", encoding="utf-8")
        res = edit_file(p, old="съобщение = 'старо'", new="съобщение = 'ново'")
        assert res.ok, res.detail
        assert p.read_text(encoding="utf-8") == "съобщение = 'ново'\n"

    def test_no_trailing_newline_is_not_invented(self, tmp_path) -> None:
        p = tmp_path / "tail.py"
        p.write_text("x = 1", encoding="utf-8")   # нарочно без \n накрая
        res = edit_file(p, old="x = 1", new="x = 2")
        assert res.ok, res.detail
        assert p.read_bytes() == b"x = 2", "не добавяй \\n, който го е нямало"


class TestNeverRaises:
    def test_no_combination_of_inputs_raises(self, sample) -> None:
        rnd = random.Random(SEED + 1)
        alphabet = string.printable + "щъьюя\r\n\t"
        for _ in range(400):
            old = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 25)))
            new = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 25)))
            try:
                edit_file(sample, old=old, new=new,
                          replace_all=rnd.choice((True, False)))
            except Exception as e:  # pragma: no cover
                raise AssertionError(
                    f"edit_file хвърли за old={old!r} new={new!r} "
                    f"(seed={SEED}): {e}") from e

    def test_a_directory_path_is_reported_not_raised(self, tmp_path) -> None:
        res = edit_file(tmp_path, old="x", new="y")
        assert not res.ok
        assert res.detail


class TestMixedAndModelSuppliedLineEndings:
    """Вторият кръг по краищата на редове. Първият фикс (запомни стила на
    файла, наложи го при запис) оправяше чистите CRLF файлове и чупеше
    смесените — същата повреда, само за друг вид файл. Файл без един стил
    няма как да получи "възстановен" стил; заменя се точно намереното.
    """

    def test_a_mixed_file_keeps_every_line_as_it_was(self, tmp_path) -> None:
        p = tmp_path / "mixed.py"
        p.write_bytes(b"alpha = 1\r\nbeta = 2\ngamma = 3\n")
        res = edit_file(p, old="beta = 2", new="beta = 22")
        assert res.ok, res.detail
        assert p.read_bytes() == b"alpha = 1\r\nbeta = 22\ngamma = 3\n", p.read_bytes()

    def test_crlf_in_the_replacement_does_not_double_up(self, tmp_path) -> None:
        """Моделът редовно връща CRLF в `new`, ако е цитирал Windows файл
        по-рано в разговора. Без привеждане това ставаше `\\r\\r\\n`."""
        p = tmp_path / "crlf.py"
        p.write_bytes(b"a = 1\r\nb = 2\r\n")
        res = edit_file(p, old="b = 2", new="b = 2\r\nc = 3")
        assert res.ok, res.detail
        assert b"\r\r\n" not in p.read_bytes(), p.read_bytes()
        assert p.read_bytes() == b"a = 1\r\nb = 2\r\nc = 3\r\n"

    def test_a_multiline_lf_anchor_matches_inside_a_crlf_file(self, tmp_path) -> None:
        """Иначе редакцията се отказва с подвеждащото "няма такъв текст" за
        файл, в който текстът си е точно там."""
        p = tmp_path / "win.py"
        p.write_bytes(b"def f():\r\n    return 1\r\n\r\ndef g():\r\n    pass\r\n")
        res = edit_file(p, old="def f():\n    return 1", new="def f():\n    return 42")
        assert res.ok, res.detail
        assert p.read_bytes() == b"def f():\r\n    return 42\r\n\r\ndef g():\r\n    pass\r\n"

    def test_an_lf_file_is_not_given_crlf_by_a_crlf_replacement(self, tmp_path) -> None:
        p = tmp_path / "unix.py"
        p.write_bytes(b"a = 1\nb = 2\n")
        res = edit_file(p, old="b = 2", new="b = 2\r\nc = 3")
        assert res.ok, res.detail
        assert b"\r" not in p.read_bytes(), p.read_bytes()
