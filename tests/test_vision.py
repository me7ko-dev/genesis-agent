"""Картинки в разговора (genesis_agent.vision, 2026-10-15): частта, пътищата,
преводът за всеки доставчик, кой модел вижда, бюджетът и историята на диска.
Само фалшиви доставчици — нито ключ, нито мрежа."""
from __future__ import annotations

import base64
import io
import json
import struct
import zlib
from collections import deque
from typing import ClassVar

import pytest

# Внесен преди фикстурите: conftest пренасочва gta.HISTORY_DIR само за вече
# внесен модул — внасяне в самия тест пишеше в истинската история (2026-10-15).
import genesis_terminal_agent as gta
from genesis_agent import brain as brain_mod
from genesis_agent import budget, sandbox, vision
from genesis_agent.brain import Brain


@pytest.fixture(autouse=True)
def _history_is_the_tests(tmp_path):
    assert str(gta.HISTORY_DIR).startswith(str(tmp_path)), gta.HISTORY_DIR


def png(w: int = 4, h: int = 3, rgb: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """Истински PNG без Pillow — за CI, където Pillow го няма."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    row = b"\x00" + bytes(rgb) * w
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * h)) + chunk(b"IEND", b""))


def _part(name: str = "shot.png") -> dict:
    return vision.image_part(png(), "image/png", name)


def _data_of(part: dict) -> bytes:
    return base64.b64decode(part["image_url"]["url"].split(",", 1)[1])


# ── частта ──────────────────────────────────────────────────────────────────

def test_image_part_is_an_openai_data_url_with_its_name_on_the_side() -> None:
    part = _part("скрийншот.png")
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")
    assert _data_of(part) == png()
    meta = part["genesis"]
    assert meta["name"] == "скрийншот.png" and meta["mime"] == "image/png"
    assert meta["width"] == 4 and meta["height"] == 3 and len(meta["sha256"]) == 64


def test_the_first_bytes_decide_not_the_hint() -> None:
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    assert vision.sniff_mime(jpeg) == "image/jpeg"
    assert vision.sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert vision.sniff_mime(b"GIF89a....") == "image/gif"
    with pytest.raises(vision.ImageError, match="не е PNG, JPEG, WebP или GIF"):
        vision.image_part(b"%PDF-1.7 not an image", "image/png", "doc.png")
    with pytest.raises(vision.ImageError, match="празен"):
        vision.image_part(b"", None, "x.png")


def test_over_20_mb_is_refused_before_anything_else() -> None:
    with pytest.raises(vision.ImageError, match="най-много 20 MB"):
        vision.image_part(b"\x89PNG\r\n\x1a\n" + b"\x00" * (vision.MAX_IMAGE_BYTES + 1))


def test_without_pillow_small_goes_as_is_and_big_says_how_to_fix(monkeypatch) -> None:
    monkeypatch.setattr(vision, "_pillow", lambda: None)
    assert _data_of(_part()) == png()
    big = png() + b"\x00" * (vision.MAX_SEND_BYTES + 10)
    with pytest.raises(vision.ImageError, match="pip install pillow"):
        vision.image_part(big, "image/png", "huge.png")


def test_with_pillow_a_big_screenshot_is_downscaled_to_1568() -> None:
    Image = pytest.importorskip("PIL.Image")
    import io
    buf = io.BytesIO()
    Image.new("RGB", (3136, 1000), (10, 120, 200)).save(buf, format="PNG")
    part = vision.image_part(buf.getvalue(), "image/png", "wide.png")
    assert (part["genesis"]["width"], part["genesis"]["height"]) == (1568, 500)
    assert Image.open(io.BytesIO(_data_of(part))).size == (1568, 500)


def test_with_pillow_a_broken_png_is_refused() -> None:
    pytest.importorskip("PIL.Image")
    with pytest.raises(vision.ImageError, match="не се отваря"):
        vision.image_part(png()[:40], "image/png", "cut.png")


def test_base64_from_the_phone_and_data_urls() -> None:
    raw = base64.b64encode(png()).decode()
    assert _data_of(vision.image_from_base64(raw, "image/png", "a.png")) == png()
    assert _data_of(vision.image_from_base64("data:image/png;base64," + raw)) == png()
    with pytest.raises(vision.ImageError, match="base64"):
        vision.image_from_base64("@@@not base64@@@", "image/png", "b.png")


def test_user_message_without_images_stays_a_plain_string() -> None:
    assert vision.user_message("здравей", []) == {"role": "user", "content": "здравей"}
    msg = vision.user_message("какво има тук?", [_part()])
    assert msg["content"][0] == {"type": "text", "text": "какво има тук?"}
    assert vision.is_image_part(msg["content"][1])
    assert vision.text_of(msg["content"]) == "какво има тук?\n[картинка: shot.png]"


# ── пътищата: като READ_FILE ─────────────────────────────────────────────────

def test_load_image_is_relative_to_the_workspace(tmp_path) -> None:
    (tmp_path / "shot.png").write_bytes(png())
    part = vision.load_image("shot.png", workspace=tmp_path)
    assert part["genesis"]["name"] == "shot.png"
    with pytest.raises(vision.ImageError, match="не съществува"):
        vision.load_image("missing.png", workspace=tmp_path)
    (tmp_path / "dir.png").mkdir()
    with pytest.raises(vision.ImageError, match="обикновен файл"):
        vision.load_image("dir.png", workspace=tmp_path)


def test_a_secret_path_is_refused_without_a_person(tmp_path, monkeypatch) -> None:
    secret = tmp_path / ".ssh"
    secret.mkdir()
    (secret / "id_rsa.png").write_bytes(png())
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    with pytest.raises(vision.ImageError, match="SANDBOX DENIED"):
        vision.load_image(str(secret / "id_rsa.png"))


def test_a_genesis_control_file_is_refused_too(tmp_path, monkeypatch) -> None:
    ctl = tmp_path / ".genesis"
    ctl.mkdir()
    (ctl / "hooks.json").write_bytes(png())
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="deny"))
    with pytest.raises(vision.ImageError, match="SANDBOX DENIED"):
        vision.load_image(str(ctl / "hooks.json"))


# ── кой вижда ────────────────────────────────────────────────────────────────

@pytest.fixture
def config(tmp_path, monkeypatch):
    """config.yaml на теста — за vision_models и `vision:` на записа."""
    path = tmp_path / "config.yaml"

    def write(models: dict) -> None:
        import yaml
        path.write_text(yaml.safe_dump({"models": models}), encoding="utf-8")
        monkeypatch.setattr(vision, "_cfg_cache", None)
    monkeypatch.setattr("genesis_agent.paths.CONFIG_PATH", path)
    write({})
    return write


# EXIF с орientация 6 и сгрешен GPS IFD (фъз 2026-10-20): Pillow хвърля TypeError
# от exif_transpose при PNG/WebP и срутваше `genesis -p @img`, `/image`, `send`.
_BAD_EXIF = bytes.fromhex(
    "4d4d002a000000080003010f0002000000024d0000000112000300000001000600008825000400000001"
    "000000320000000000d800020005000000030000004400000000000000010000000100000002000000010000000300000001")


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("WEBP", "image/webp"), ("JPEG", "image/jpeg")])
def test_broken_exif_is_a_picture_without_exif_not_a_crash(fmt, mime) -> None:
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 200, 10)).save(buf, format=fmt, exif=_BAD_EXIF)
    part = vision.image_part(buf.getvalue(), mime, f"bad.{fmt.lower()}")
    assert (part["genesis"]["width"], part["genesis"]["height"]) == (64, 48)
    out = Image.open(io.BytesIO(_data_of(part)))
    assert not out.getexif(), "прекодирана без съмнителните метаданни"


def test_known_names_see_and_text_models_do_not(config) -> None:
    for provider, model in [("groq", "meta-llama/llama-4-scout-17b-16e-instruct"),
                            ("openrouter", "google/gemma-4-31b-it:free"),
                            ("anthropic", "claude-sonnet-5"), ("openai", "gpt-5.5"),
                            ("openai", "gpt-4o-mini"), ("gemini", "gemini-2.5-flash"),
                            ("vertex", "google/gemini-2.5-pro"), ("nvidia", "qwen/qwen2.5-vl-72b"),
                            ("openrouter", "mistralai/pixtral-large"),
                            ("ollama_local", "llama3.2-vision:11b")]:
        assert vision.can_see(provider, model), (provider, model)
    for provider, model in [("groq", "openai/gpt-oss-120b"), ("ollama_cloud", "gpt-oss:120b-cloud"),
                            ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"),
                            # само текст въпреки името (2026-10-20)
                            ("ollama_local", "gemma3:1b"), ("openrouter", "google/gemma-3-1b-it"),
                            ("gemini", "gemini-embedding-001"),
                            ("deepseek", "deepseek-chat"), ("huggingface", "Qwen/Qwen2.5-72B-Instruct")]:
        assert not vision.can_see(provider, model), (provider, model)


def test_config_decides_over_the_name(config) -> None:
    config({"vision_models": ["groq/openai/gpt-oss-120b"],
            "fallback_models": [{"provider": "openrouter", "model": "google/gemma-4-31b-it:free",
                                 "vision": False}]})
    assert vision.can_see("groq", "openai/gpt-oss-120b")
    assert not vision.can_see("openrouter", "google/gemma-4-31b-it:free")


def test_the_claude_subscription_sees_only_when_listed(config) -> None:
    # Не е проверено с истински абонамент — само по изричен избор (2026-10-15).
    assert not vision.can_see("claude_cli", "sonnet")
    config({"vision_models": ["sonnet"]})
    assert not vision.can_see("claude_cli", "sonnet"), "само model не стига за абонамента"
    config({"vision_models": ["claude_cli/sonnet"]})
    assert vision.can_see("claude_cli", "sonnet")


# ── преводът за всеки доставчик ──────────────────────────────────────────────

def _history(text: str = "какво има тук?") -> list[dict]:
    return [{"role": "system", "content": "s"}, vision.user_message(text, [_part()])]


def test_openai_compatible_gets_clean_parts_without_the_genesis_field() -> None:
    out = vision.for_provider(_history(), sees=True)
    part = out[1]["content"][1]
    assert part == {"type": "image_url", "image_url": {"url": _history()[1]["content"][1]
                                                         ["image_url"]["url"]}}
    # Входът не се пипа — историята на чата остава с името.
    assert "genesis" in _history()[1]["content"][1]


def test_anthropic_gets_base64_image_blocks() -> None:
    _system, msgs = Brain._to_anthropic_messages(_history())
    blocks = msgs[0]["content"]
    assert blocks[0] == {"type": "text", "text": "какво има тук?"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(blocks[1]["source"]["data"]) == png()


def test_gemini_native_part_is_inline_data() -> None:
    # Brain вика Gemini/Vertex през OpenAI-съвместимия им адрес (image_url);
    # родният формат е за клиент на generateContent.
    p = vision.gemini_part(_part())
    assert p["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(p["inline_data"]["data"]) == png()


def test_claude_cli_stream_json_carries_the_image_block() -> None:
    from genesis_agent import claude_cli
    system, blocks = claude_cli.render_content(_history())
    assert system == "s"
    assert blocks[0] == "какво има тук?" and blocks[1]["type"] == "image"
    line = json.loads(claude_cli.stdin_message(blocks))
    content = line["message"]["content"]
    assert content[0] == {"type": "text", "text": "какво има тук?"}
    assert content[1]["source"]["media_type"] == "image/png"
    # Текстовите блокове (за кеша по граници) са същите като преди.
    assert claude_cli.render_blocks(_history())[1] == ["какво има тук?"]


def test_a_model_that_cannot_see_gets_a_note_and_a_plain_string() -> None:
    out = vision.for_provider(_history(), sees=False)
    assert isinstance(out[1]["content"], str)
    assert "[картинка: shot.png — нито един модел във веригата не вижда картинки" in out[1]["content"]
    assert "base64" not in json.dumps(out)


# ── Brain: маршрутът на заявка с картинка ────────────────────────────────────

class _Resp:
    status_code = 200
    text = ""

    def __init__(self, content: str = "виждам червен квадрат") -> None:
        self._content = content

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}, "finish_reason": "stop"}]}


@pytest.fixture
def wire(monkeypatch, config):
    """Brain с фалшива верига; всяка заявка по „мрежата“ се записва."""
    sent: list[dict] = []
    fail: set[str] = set()

    def post(url, *, headers, json, timeout):
        sent.append({"url": url, **json})
        if json["model"] in fail:
            raise brain_mod.requests.exceptions.ConnectionError("мрежата падна")
        return _Resp()

    monkeypatch.setattr(brain_mod, "_post_with_deadline", post)
    monkeypatch.setattr(brain_mod, "_load_keys",
                        lambda: {"GROQ_API_KEY": "k", "OPENROUTER_API_KEY": "k",
                                 "GEMINI_API_KEY": "k"})
    monkeypatch.setattr(brain_mod, "_ollama_installed", list)
    monkeypatch.setattr(brain_mod.time, "sleep", lambda s: None)

    def make(chain: list[tuple[str, str, float]], **kw) -> Brain:
        entries = [{"provider": p, "model": m, "size_b": s, "supports_tools": False}
                   for p, m, s in chain]
        monkeypatch.setattr(brain_mod, "_load_chain", lambda: [dict(e) for e in entries])
        b = Brain(use_local=False, **kw)
        b.local = None
        return b
    return make, sent, fail


def _image_parts(payload: dict) -> list[dict]:
    return [p for m in payload["messages"] if isinstance(m.get("content"), list)
            for p in m["content"] if p.get("type") == "image_url"]


def test_an_image_goes_to_the_first_model_that_sees(wire, capsys) -> None:
    make, sent, _ = wire
    b = make([("groq", "openai/gpt-oss-120b", 120),
              ("openrouter", "google/gemma-4-31b-it:free", 31)])
    reply = b.complete(_history())
    assert reply.raw_text == "виждам червен квадрат"
    assert [s["model"] for s in sent] == ["google/gemma-4-31b-it:free"]
    parts = _image_parts(sent[0])
    assert len(parts) == 1 and set(parts[0]) == {"type", "image_url"}
    assert b.current["model"] == "google/gemma-4-31b-it:free"
    assert "👁 картинка → openrouter/google/gemma-4-31b-it:free" in capsys.readouterr().out


def test_text_only_requests_keep_the_chain_order(wire) -> None:
    make, sent, _ = wire
    b = make([("groq", "openai/gpt-oss-120b", 120),
              ("openrouter", "google/gemma-4-31b-it:free", 31)])
    b.complete([{"role": "system", "content": "s"}, {"role": "user", "content": "здрасти"}])
    assert [s["model"] for s in sent] == ["openai/gpt-oss-120b"]


def test_the_terminal_size_filter_does_not_hide_the_only_seeing_model(wire) -> None:
    # Терминалът е min_size_b=32; gemma-4-31b е единственият безплатен vision модел.
    make, sent, _ = wire
    b = make([("groq", "openai/gpt-oss-120b", 120),
              ("openrouter", "google/gemma-4-31b-it:free", 31)], min_size_b=32)
    assert [c["model"] for c in b.chain] == ["openai/gpt-oss-120b"]
    b.complete(_history())
    assert sent[0]["model"] == "google/gemma-4-31b-it:free" and _image_parts(sent[0])


def test_the_image_outranks_a_pin_that_cannot_see(wire) -> None:
    make, sent, _ = wire
    b = make([("openrouter", "google/gemma-4-31b-it:free", 31)],
             pin_model=("groq", "openai/gpt-oss-120b"))
    b.complete(_history())
    assert sent[0]["model"] == "google/gemma-4-31b-it:free"


def test_no_seeing_model_sends_a_note_instead_of_something_rejected(wire, capsys) -> None:
    make, sent, _ = wire
    b = make([("groq", "openai/gpt-oss-120b", 120)])
    b.complete(_history())
    assert len(sent) == 1 and not _image_parts(sent[0])
    user = sent[0]["messages"][-1]["content"]
    assert isinstance(user, str) and "добави vision модел в config.yaml" in user
    assert "data:image" not in json.dumps(sent[0])
    assert "картинката не се праща" in capsys.readouterr().out


def test_when_the_seeing_model_fails_the_rest_answer_with_a_note(wire) -> None:
    make, sent, fail = wire
    fail.add("google/gemma-4-31b-it:free")
    b = make([("groq", "openai/gpt-oss-120b", 120),
              ("openrouter", "google/gemma-4-31b-it:free", 31)])
    b.complete(_history())
    last = sent[-1]
    assert last["model"] == "openai/gpt-oss-120b" and not _image_parts(last)
    assert "моделите, които виждат картинки, не отговориха" in last["messages"][-1]["content"]


def test_gemini_and_vertex_get_image_url_on_their_openai_endpoint(wire) -> None:
    make, sent, _ = wire
    b = make([("gemini", "gemini-2.5-flash", 0)])
    b.complete(_history())
    assert "generativelanguage.googleapis.com" in sent[0]["url"]
    assert _image_parts(sent[0])[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_anthropic_call_sends_image_blocks(wire, monkeypatch) -> None:
    captured: dict = {}

    class _Final:
        content: ClassVar[list] = [type("B", (), {"type": "text", "text": "виждам"})()]
        stop_reason = "end_turn"
        usage = None

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return _Final()

    class _Messages:
        def stream(self, **kw):
            captured.update(kw)
            return _Stream()

    class _Client:
        def __init__(self, **kw):
            self.beta = type("Beta", (), {"messages": _Messages()})()
            self.messages = _Messages()

    import types
    fake = types.SimpleNamespace(Anthropic=_Client, BadRequestError=type("E1", (Exception,), {}),
                                 AuthenticationError=type("E2", (Exception,), {}),
                                 PermissionDeniedError=type("E3", (Exception,), {}),
                                 RateLimitError=type("E4", (Exception,), {}),
                                 APIStatusError=type("E5", (Exception,), {}),
                                 APIConnectionError=type("E6", (Exception,), {}))
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake)
    b = Brain.__new__(Brain)
    b.timeout = 5
    text, _ = Brain._call_anthropic(b, "k", "claude-sonnet-5",
                                    vision.for_provider(_history(), True), None, "")
    assert text == "виждам"
    block = captured["messages"][0]["content"][1]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"


# ── бюджетът и кешът ─────────────────────────────────────────────────────────

def test_an_image_counts_as_about_1500_tokens_not_its_base64() -> None:
    msg = vision.user_message("виж", [vision.image_part(png(400, 400))])
    assert len(json.dumps(msg)) > 1000
    assert vision.content_chars(msg["content"]) == len("виж") + vision.IMAGE_CHARS
    from genesis_agent import context_usage
    assert context_usage._text(msg["content"]) == len("виж") + vision.IMAGE_CHARS


def test_cache_history_does_not_drop_the_conversation_for_one_photo() -> None:
    # Без броенето по IMAGE_CHARS: 1 MB base64 > таван → историята падаше до задачата.
    big = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 1_000_000},
           "genesis": {"name": "big.png"}}
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "първо"}, {"role": "assistant", "content": "добре"},
            {"role": "user", "content": [{"type": "text", "text": "виж"}, big]}]
    assert budget.cache_history(msgs, max_chars=50_000) == msgs
    assert budget.cache_stable_history(msgs, max_chars=50_000) == msgs


def test_old_images_are_elided_after_three_operator_turns() -> None:
    msgs: list[dict] = [{"role": "system", "content": "s"},
                        vision.user_message("първа", [_part("old.png")])]
    for i in range(3):
        msgs += [{"role": "assistant", "content": f"отговор {i}"},
                 {"role": "user", "content": f"въпрос {i}"}]
    out = vision.elide_old_images(msgs)
    assert isinstance(out[1]["content"], str)
    assert "[картинка: old.png — от по-ранен ход" in out[1]["content"]
    assert vision.has_images(msgs), "историята на извикващия остава непокътната"
    # Два хода по-късно картинката още е в контекста.
    assert vision.has_images(vision.elide_old_images(msgs[:-2]))


def test_at_most_eight_images_go_in_one_request() -> None:
    msgs = [{"role": "system", "content": "s"},
            vision.user_message("много", [_part(f"{i}.png") for i in range(10)])]
    out = vision.elide_old_images(msgs)
    assert len(vision.images_in(out[1]["content"])) == vision.MAX_IMAGES_PER_REQUEST


def test_compaction_never_puts_base64_in_the_summary_prompt(monkeypatch) -> None:
    seen: list[str] = []

    def fake_complete(self, messages, tools=None, avoid=None):
        seen.append(messages[-1]["content"])
        return type("R", (), {"raw_text": "резюме", "code": "", "usage": None, "tool_calls": None})
    monkeypatch.setattr(Brain, "complete", fake_complete)
    msgs = [{"role": "system", "content": "s"}, vision.user_message("виж логото", [_part("logo.png")])]
    msgs += [{"role": "user" if i % 2 else "assistant", "content": f"реплика {i}"} for i in range(20)]
    out = Brain.compact_chat_history(msgs, threshold=16, keep_recent=4)
    assert len(out) < len(msgs) and seen
    assert "[картинка: logo.png]" in seen[0] and "base64" not in seen[0]


def test_the_attachment_is_not_mistaken_for_the_task() -> None:
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "направи сайта"}]
    msgs += [{"role": "assistant", "content": f"a{i}"} for i in range(40)]
    msgs.append(vision.attachment_message([_part()], "VIEW_IMAGE: shot.png"))
    window = budget.request_window(msgs, limit=10)
    assert {"role": "user", "content": "направи сайта"} in window


# ── историята на диска ───────────────────────────────────────────────────────

def test_the_session_file_holds_a_reference_and_resume_brings_the_image_back(tmp_path) -> None:

    photo = vision.image_part(png(300, 300), "image/png", "photo.png")
    messages = deque([{"role": "system", "content": "s"},
                      vision.user_message("какво е това?", [photo]),
                      {"role": "assistant", "content": "котка"}])
    gta._save_session(messages)
    saved_text = gta._session_file().read_text(encoding="utf-8")
    assert "base64" not in saved_text and len(saved_text) < 2000
    assert len(list((gta.HISTORY_DIR / "images").iterdir())) == 1
    loaded = json.loads(saved_text)
    assert loaded[1]["content"][1]["image_url"]["url"].startswith(vision.REF_SCHEME)

    restored = gta._restore_session(loaded, "нов промпт")
    part = restored[1]["content"][1]
    assert part["image_url"]["url"] == photo["image_url"]["url"]
    assert part["genesis"]["name"] == "photo.png"


def test_saving_twice_stores_the_bytes_once(tmp_path) -> None:

    msg = vision.user_message("x", [_part()])
    gta._save_session([{"role": "system", "content": "s"}, msg])
    gta._save_session([{"role": "system", "content": "s"}, msg, msg])
    assert len(list((gta.HISTORY_DIR / "images").iterdir())) == 1


def test_resume_without_the_image_file_still_loads_with_a_note(tmp_path) -> None:

    gta._save_session([{"role": "system", "content": "s"}, vision.user_message("x", [_part()])])
    for f in (gta.HISTORY_DIR / "images").iterdir():
        f.unlink()
    loaded = json.loads(gta._session_file().read_text(encoding="utf-8"))
    restored = gta._restore_session(loaded, "p")
    assert restored[1]["content"] == "x\n[картинка: shot.png — файлът ѝ вече го няма в историята]"


def test_resume_command_restores_images_from_next_to_the_session_file(tmp_path) -> None:
    from genesis_agent import resume
    store = tmp_path / "images"
    saved = vision.for_disk([{"role": "system", "content": "s"},
                             vision.user_message("снимка", [_part()])], store)
    session = tmp_path / "session_1.json"
    session.write_text(json.dumps(saved), encoding="utf-8")
    from genesis_agent.agent_core import restored_history
    restored = restored_history(saved, "p", image_store=session.parent / "images")
    assert vision.has_images(restored)
    assert resume._is_operator(restored[1]), "реплика с картинка е точка за /rewind"
    assert resume._short(restored[1]["content"]) == "снимка [картинка: shot.png]"


def test_the_session_list_shows_the_text_of_a_message_with_an_image(tmp_path) -> None:
    from genesis_agent import session_index
    path = tmp_path / "session_x.json"
    path.write_text(json.dumps(vision.for_disk(
        [{"role": "system", "content": "s"}, vision.user_message("логото е криво", [_part()])],
        tmp_path / "images")), encoding="utf-8")
    assert session_index.describe(path).first.startswith("логото е криво")


def test_the_store_is_capped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(vision, "_STORE_MAX_FILES", 3)
    store = tmp_path / "images"
    for i in range(5):
        vision.for_disk([vision.user_message("x", [vision.image_part(png(1, 1, (i, i, i)))])], store)
    assert len(list(store.iterdir())) == 3


def test_desktop_replay_shows_the_image_name_not_its_bytes() -> None:
    from genesis_agent import desktop_commands
    events: list[tuple] = []
    desktop_commands.replay(lambda kind, **d: events.append((kind, d)),
                            [{"role": "system", "content": "s"},
                             vision.user_message("виж", [_part()]),
                             vision.attachment_message([_part("tool.png")], "VIEW_IMAGE: tool.png"),
                             {"role": "assistant", "content": "ок"}])
    assert events[1] == ("user", {"text": "виж", "images": [
        {"name": "shot.png", "mime": "image/png", "bytes": len(png()), "width": 4, "height": 3}]})
    assert [e[0] for e in events] == ["cleared", "user", "assistant"]
    assert "base64" not in json.dumps(events)


def test_the_status_bar_estimate_counts_an_image_as_1500_tokens() -> None:

    gta.reset_usage()
    msg = vision.user_message("виж", [vision.image_part(png(500, 500))])
    gta.count_usage(None, [msg], "")
    assert gta.total_input_tokens == (len("виж") + vision.IMAGE_CHARS) // 4
    gta.reset_usage()


def test_conversation_memory_sees_a_note_not_base64() -> None:
    from genesis_agent import workspace_memory
    text = workspace_memory.operator_text(vision.user_message("запомни логото", [_part()])["content"])
    assert text == "запомни логото\n[картинка: shot.png]"


# ── ревю 2026-10-15 ──────────────────────────────────────────────────────────

def test_a_small_rotated_phone_photo_is_sent_upright_and_without_its_location() -> None:
    """Под 1568 px и 5 MB байтовете тръгваха както са: EXIF ориентацията
    оставаше (моделът я вижда легнала, а width/height казваха изправена), а с
    нея и GPS координатите на снимката — към чужд доставчик."""
    Image = pytest.importorskip("PIL.Image")
    import io
    exif = Image.Exif()
    exif[0x0112] = 6                                   # завъртяна на 90°
    exif[0x8825] = {2: (42.0, 41.0, 0.0)}              # GPS
    buf = io.BytesIO()
    Image.new("RGB", (200, 100), (255, 0, 0)).save(buf, format="JPEG", exif=exif.tobytes())
    part = vision.image_part(buf.getvalue(), "image/jpeg", "photo.jpg")
    sent = Image.open(io.BytesIO(_data_of(part)))
    assert sent.size == (100, 200) == (part["genesis"]["width"], part["genesis"]["height"])
    assert not sent.getexif().get_ifd(0x8825) and sent.getexif().get(0x0112, 1) == 1


def test_a_plain_small_image_still_goes_byte_for_byte() -> None:
    pytest.importorskip("PIL.Image")
    assert _data_of(vision.image_part(png(40, 30), "image/png", "a.png")) == png(40, 30)


def test_a_session_ref_is_only_a_hash_name_in_the_store(tmp_path) -> None:
    """Препратката в записания разговор е `<sha256>.<ext>` в images/. Друго
    име (`CON`/`COM1` на Windows е устройство — /resume би чакал конзолата;
    FIFO висеше) не се отваря изобщо; `../` така или иначе не излизаше."""
    store = tmp_path / "images"
    store.mkdir()
    data = png()
    sha = __import__("hashlib").sha256(data).hexdigest()
    (store / "CON").write_bytes(data)
    (tmp_path / "outside.png").write_bytes(data)
    meta = {"name": "x.png", "mime": "image/png", "sha256": sha}
    for ref in ("CON", "../outside.png", "x.png"):
        msg = {"role": "user", "content": [
            {"type": "text", "text": "t"},
            {"type": "image_url", "image_url": {"url": vision.REF_SCHEME + ref}, "genesis": meta}]}
        assert vision.from_disk([msg], store)[0]["content"] == \
            "t\n[картинка: x.png — файлът ѝ вече го няма в историята]", ref


def test_a_hand_edited_session_with_odd_image_parts_still_loads(tmp_path) -> None:
    """`image_url` като низ (форматът на някои клиенти), `genesis` не речник:
    from_disk/for_disk гърмяха с AttributeError/ValueError — /resume падаше."""
    odd = {"role": "user", "content": [
        {"type": "text", "text": "t"},
        {"type": "image_url", "image_url": "genesis-image:abc.png"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}, "genesis": "x"}]}
    loaded = vision.from_disk([odd], tmp_path)
    assert loaded[0]["role"] == "user"
    vision.for_disk(loaded, tmp_path / "images")


def test_text_tag_models_learn_view_image_only_when_it_is_offered(monkeypatch) -> None:
    """Моделите без native инструменти четат таговете от tool_tag_prompt —
    там VIEW_IMAGE липсваше, т.е. виждащ модел в текстов режим не знаеше, че
    го има. Ред само когато инструментът е предложен: иначе е разход."""
    monkeypatch.setattr(Brain, "_TAG_PROMPT_CACHE", "TOOLS:\n- [READ_FILE: path]\n\nMEMORY TAGS:\n- x")
    history = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    read_file = {"type": "function", "function": {"name": "READ_FILE", "parameters": {}}}
    offered = Brain._with_tool_tag_docs(history, [read_file, vision.TOOL_SCHEMA])[0]["content"]
    assert "[VIEW_IMAGE: " in offered
    assert offered.index("[VIEW_IMAGE: ") < offered.index("MEMORY TAGS:")
    for tools in ([read_file], None):
        assert "VIEW_IMAGE" not in Brain._with_tool_tag_docs(history, tools)[0]["content"]


def test_a_text_tag_model_in_the_chain_gets_the_view_image_line(wire, monkeypatch) -> None:
    """През Brain.complete: инструментите от хода стигат до документацията."""
    monkeypatch.setattr(Brain, "_TAG_PROMPT_CACHE", "TOOLS:\n- [READ_FILE: path]")
    make, sent, _ = wire
    b = make([("groq", "openai/gpt-oss-120b", 120)])
    b.complete([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
               tools=[vision.TOOL_SCHEMA])
    assert "[VIEW_IMAGE: " in sent[0]["messages"][0]["content"]


def test_a_page_screenshot_over_pillows_bomb_limit_is_skipped_not_a_crash(tmp_path) -> None:
    """Снимка на цялата страница над 2× Image.MAX_IMAGE_PIXELS: Image.open
    хвърля DecompressionBombError (не е OSError) — _top_of я пускаше нагоре и
    ходът падаше в проверката в браузър."""
    pytest.importorskip("PIL.Image")
    from genesis_agent import page_check

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    tall = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1440, 130_000, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b""))
    (tmp_path / "desktop-light.png").write_bytes(tall)
    (tmp_path / "mobile-light.png").write_bytes(png(39, 80))
    shots = page_check._shots_of(tmp_path, "index.html")
    assert [vision.image_name(p) for p in shots] == ["index.html mobile-light.png"]
