"""genesis_agent.voice (2026-10-15): говор → текст, текст → говор, микрофон.

Всичко е с фалшиви доставчици — без мрежа, без ключове, без истински
микрофон и високоговорител (conftest._no_real_audio спира всяко стигане до тях).
"""
from __future__ import annotations

import asyncio
import importlib.machinery
import io
import struct
import sys
import threading
import types
import wave

import pytest
import requests

from genesis_agent import budget, voice
from genesis_agent.voice import Settings, VoiceError

GROQ_KEY = "gsk_" + "Q1w2E3r4" * 5
GROQ_KEY_2 = "gsk_" + "Z9x8C7v6" * 5


def _wav(seconds: float, level: int = 0, rate: int = voice.SAMPLE_RATE) -> bytes:
    n = int(seconds * rate)
    return voice.pcm_to_wav(struct.pack(f"<{n}h", *([level] * n)), rate)


class _Reply:
    def __init__(self, status: int, body: object = None, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text or (str(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture()
def calls(monkeypatch):
    """Фалшивият `requests.post`: записва заявките, отговаря от опашка."""
    sent: list[dict] = []
    replies: list[object] = []

    def post(url, headers=None, data=None, files=None, timeout=None, **kw):
        sent.append({"url": url, "headers": headers or {}, "data": data or {}, "files": files or {},
                     "timeout": timeout})
        reply = replies.pop(0) if replies else _Reply(200, {"text": "здравей", "duration": 1.0})
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(requests, "post", post)
    for i in ["", *[f"_{n}" for n in range(2, 11)]]:
        monkeypatch.delenv(f"GROQ_API_KEY{i}", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return sent, replies


def _cfg(**kw) -> Settings:
    return Settings(**kw)


# ── говор → текст: Groq ─────────────────────────────────────────────────────

def test_groq_gets_an_openai_style_multipart_request_with_the_chain_key(monkeypatch, calls) -> None:
    sent, replies = calls
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    replies.append(_Reply(200, {"text": " Направи тест за voice. ", "duration": 2.5, "language": "bg"}))
    audio = _wav(2.5)
    got = voice.transcribe_detail(audio, "audio/wav", "bg", cfg=_cfg(stt_backends=("groq",)))
    assert got.text == "Направи тест за voice." and got.provider == "groq"
    (req,) = sent
    assert req["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert req["headers"] == {"Authorization": f"Bearer {GROQ_KEY}"}
    assert req["data"] == {"model": "whisper-large-v3", "temperature": "0",
                           "response_format": "verbose_json", "language": "bg"}
    name, body, mime = req["files"]["file"]
    assert (name, mime) == ("audio.wav", "audio/wav") and body == audio
    assert req["timeout"] == 60.0


def test_the_audio_seconds_reach_genesis_budget(monkeypatch, calls) -> None:
    _, replies = calls
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    replies.append(_Reply(200, {"text": "да", "duration": 4.25}))
    voice.transcribe_detail(_wav(4.25), cfg=_cfg(stt_backends=("groq",)))
    day = budget.today_totals()
    assert day["audio_seconds"] == 4.25
    assert day["by_provider"]["groq"] == {"calls": 1, "total_tokens": 0, "audio_seconds": 4.25}
    report = budget.format_report(day, title="Днес")
    assert "Глас → текст: 4 s аудио" in report and "4 s аудио" in report.splitlines()[-1]


def test_the_budget_report_without_voice_is_unchanged() -> None:
    budget.record_usage(provider="groq", model="m", prompt_tokens=1, completion_tokens=2)
    day = budget.today_totals()
    assert "audio_seconds" not in day and day["by_provider"]["groq"] == {"calls": 1, "total_tokens": 3}
    assert "аудио" not in budget.format_report(day, title="Днес")


def test_a_rate_limited_key_rotates_to_the_next_and_no_key_is_ever_shown(monkeypatch, calls) -> None:
    sent, replies = calls
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    monkeypatch.setenv("GROQ_API_KEY_2", GROQ_KEY_2)
    replies += [_Reply(429, text=f"rate limit for key {GROQ_KEY}"), _Reply(200, {"text": "ок"})]
    assert voice.transcribe(_wav(1)) == "ок"           # публичното API: config.yaml, groq е пръв
    assert [r["headers"]["Authorization"] for r in sent] == [f"Bearer {GROQ_KEY}", f"Bearer {GROQ_KEY_2}"]
    sent.clear()
    replies += [_Reply(401, text=f"bad key {GROQ_KEY}"), _Reply(429, text=f"Authorization: Bearer {GROQ_KEY_2}"),
                _Reply(429, text="x"), _Reply(429, text="y")]
    with pytest.raises(VoiceError) as e:
        voice.transcribe_detail(_wav(1), cfg=_cfg(stt_backends=("groq",)))
    msg = str(e.value)
    assert GROQ_KEY not in msg and GROQ_KEY_2 not in msg
    assert "ключ №1" in msg and "ключ №2" in msg and "HTTP 401" in msg
    # двата модела × двата ключа
    assert [r["data"]["model"] for r in sent] == ["whisper-large-v3"] * 2 + ["whisper-large-v3-turbo"] * 2


def test_a_bad_request_moves_to_the_next_model_not_the_next_key(monkeypatch, calls) -> None:
    sent, replies = calls
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    monkeypatch.setenv("GROQ_API_KEY_2", GROQ_KEY_2)
    replies += [_Reply(400, text="model decommissioned"), _Reply(200, {"text": "турбо"})]
    got = voice.transcribe_detail(_wav(1), cfg=_cfg(stt_backends=("groq",)))
    assert got.text == "турбо" and got.model == "whisper-large-v3-turbo"
    assert [r["headers"]["Authorization"] for r in sent] == [f"Bearer {GROQ_KEY}"] * 2


def test_no_network_falls_through_to_the_next_backend(monkeypatch, calls) -> None:
    _, replies = calls
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    replies.append(requests.ConnectionError("DNS"))
    seen: list[str] = []

    def local(audio, mime, language, cfg):
        seen.append(language)
        return voice.Transcript("офлайн", "local", "fake", 0.0)

    monkeypatch.setitem(voice.STT_BACKENDS, "local", local)
    got = voice.transcribe_detail(_wav(1.5), language="bg", cfg=_cfg(stt_backends=("groq", "local")))
    assert got.text == "офлайн" and seen == ["bg"]
    assert got.seconds == pytest.approx(1.5)          # от WAV заглавката, доставчикът не каза


def test_nothing_configured_is_one_clear_bulgarian_error(calls) -> None:
    sent, _ = calls
    with pytest.raises(VoiceError) as e:
        voice.transcribe_detail(_wav(1), cfg=_cfg())
    msg = str(e.value)
    assert "GROQ_API_KEY" in msg and "voice-local" in msg and "groq: няма GROQ_API_KEY" in msg
    assert "openai: не е настроен" in msg and "faster-whisper не е инсталиран" in msg
    assert sent == []                                  # нищо не е пратено никъде


def test_the_openai_backend_is_used_only_when_configured(monkeypatch, calls) -> None:
    sent, replies = calls
    monkeypatch.setenv("MY_STT_KEY", "sk-" + "a" * 30)
    replies.append(_Reply(200, {"text": "общ адрес"}))
    cfg = _cfg(stt_backends=("openai",), openai_base_url="https://stt.example/v1/",
               openai_key_env="MY_STT_KEY", openai_model="gpt-4o-mini-transcribe")
    assert voice.transcribe_detail(_wav(1), "audio/webm;codecs=opus", cfg=cfg).text == "общ адрес"
    (req,) = sent
    assert req["url"] == "https://stt.example/v1/audio/transcriptions"
    assert req["data"]["response_format"] == "json" and req["data"]["model"] == "gpt-4o-mini-transcribe"
    assert req["files"]["file"][0] == "audio.webm"


def _fake_faster_whisper(monkeypatch, got: list) -> None:
    mod = types.ModuleType("faster_whisper")
    mod.__spec__ = importlib.machinery.ModuleSpec("faster_whisper", None)

    class WhisperModel:
        def __init__(self, size, device, compute_type):
            got.append(("init", size))

        def transcribe(self, audio, language=None, beam_size=5, vad_filter=False):
            got.append(("audio", type(audio).__name__, language))
            seg = types.SimpleNamespace
            return iter([seg(text=" Здравей,"), seg(text=" свят. ")]), seg(duration=2.0)

    mod.WhisperModel = WhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)


def test_local_faster_whisper_reads_from_memory_and_loads_the_model_once(monkeypatch, calls) -> None:
    got: list = []
    _fake_faster_whisper(monkeypatch, got)
    cfg = _cfg(stt_backends=("local",), local_model="tiny")
    first = voice.transcribe_detail(_wav(2), cfg=cfg)
    voice.transcribe_detail(_wav(2), cfg=cfg)
    assert first.text == "Здравей, свят." and first.provider == "local" and first.seconds == 2.0
    assert got.count(("init", "tiny")) == 1
    assert ("audio", "BytesIO", "bg") in got           # не път към файл — нищо на диска


@pytest.mark.parametrize("audio,mime,word", [
    (b"", "audio/wav", "празен"),
    (b"x" * (voice.MAX_AUDIO_BYTES + 1), "audio/m4a", "MB"),
    (None, "audio/wav", "таванът е 120 s"),
    (b"abc", "video/mp4", "Непознат аудио формат"),
])
def test_limits_are_checked_before_anything_is_sent(calls, audio, mime, word) -> None:
    sent, _ = calls
    audio = _wav(125) if audio is None else audio
    with pytest.raises(VoiceError, match=word):
        voice.transcribe_detail(audio, mime, cfg=_cfg(stt_backends=("groq",)))
    assert sent == []


def test_m4a_duration_comes_from_the_mvhd_box() -> None:
    mvhd0 = b"mvhd" + b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, 7500)
    assert voice.audio_seconds(b"\x00" * 40 + b"moov" + mvhd0, "audio/m4a") == 7.5
    mvhd1 = b"mvhd" + b"\x01\x00\x00\x00" + struct.pack(">QQIQ", 0, 0, 600, 600 * 130)
    assert voice.audio_seconds(mvhd1, "audio/mp4") == 130.0
    assert voice.audio_seconds(b"no box", "audio/m4a") is None
    assert voice.audio_seconds(b"webm", "audio/webm") is None


def test_settings_come_from_config_yaml_and_env(monkeypatch, tmp_path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("voice:\n  language: en\n  stt:\n    backends: [local]\n"
                        "  tts:\n    voice: bg-BG-BorislavNeural\n    max_sentences: 2\n", encoding="utf-8")
    monkeypatch.setattr("genesis_agent.paths.CONFIG_PATH", cfg_file)
    s = voice.settings()
    assert (s.language, s.stt_backends, s.tts_voice, s.max_sentences) == \
        ("en", ("local",), "bg-BG-BorislavNeural", 2)
    monkeypatch.setenv("GENESIS_STT_BACKENDS", "groq, local")
    monkeypatch.setenv("GENESIS_VOICE_SPEAK", "1")
    s = voice.settings()
    assert s.stt_backends == ("groq", "local") and s.speak_answers is True
    cfg_file.write_text("voice: [broken", encoding="utf-8")
    assert voice.settings().language == "bg"


def test_the_shipped_config_has_a_voice_section() -> None:
    s = voice.settings()
    assert s.language == "bg" and s.stt_backends[0] == "groq" and s.speak_answers is False
    assert s.tts_voice == "bg-BG-KalinaNeural"


# ── текст за изговаряне ─────────────────────────────────────────────────────

def test_markdown_and_code_are_not_read_aloud() -> None:
    text = ("## Готово\n\n**Тестовете** минават — виж [отчета](https://x.y/r) и `pytest -q`.\n\n"
            "```python\ndef f():\n    return 1\n```\n\n- първо\n- второ\n\nhttps://example.com/долго")
    spoken = voice.speech_text(text, max_sentences=10, max_chars=1000)
    for junk in ("#", "**", "`", "](", "def f", "return 1", "https://", "- "):
        assert junk not in spoken, junk
    assert spoken.startswith("Готово. Тестовете минават")
    assert "отчета" in spoken and "pytest -q" in spoken and "първо. второ." in spoken
    assert "линк" in spoken and spoken.endswith(voice.CODE_ON_SCREEN)


def test_a_table_is_read_as_its_cells() -> None:
    spoken = voice.speech_text("| файл | редове |\n|---|---|\n| voice.py | 900 |", max_sentences=9,
                               max_chars=500)
    assert spoken == "файл, редове. voice.py, 900."


def test_a_long_answer_is_cut_to_the_first_sentences() -> None:
    text = " ".join(f"Изречение номер {i}." for i in range(1, 20))
    spoken = voice.speech_text(text, max_sentences=3, max_chars=600)
    assert spoken == ("Изречение номер 1. Изречение номер 2. Изречение номер 3. "
                      "Останалото е на екрана.")
    long_one = "дума " * 400
    spoken = voice.speech_text(long_one, max_sentences=3, max_chars=100)
    assert len(spoken) < 100 + len(voice.REST_ON_SCREEN) + 3 and spoken.endswith(voice.REST_ON_SCREEN)


def test_only_code_means_nothing_but_the_note() -> None:
    assert voice.speech_text("```\nls\n```", max_sentences=3, max_chars=100) == voice.CODE_ON_SCREEN
    assert voice.speech_text("   ", max_sentences=3, max_chars=100) == ""


# ── текст → говор ───────────────────────────────────────────────────────────

def _no_tts_installed(monkeypatch, *, espeak: str | None = None) -> None:
    real = voice.find_spec
    monkeypatch.setattr(voice, "find_spec",
                        lambda name: None if name in ("edge_tts", "pyttsx3", "faster_whisper",
                                                      "sounddevice") else real(name))
    monkeypatch.setattr(voice.shutil, "which", lambda name: espeak if name == "espeak-ng" else None)


def test_without_any_tts_the_error_says_what_to_install(monkeypatch) -> None:
    _no_tts_installed(monkeypatch)
    with pytest.raises(VoiceError) as e:
        voice.synthesize("Здравей", cfg=_cfg())
    assert voice.INSTALL_HINT in str(e.value) and "edge-tts не е инсталиран" in str(e.value)
    assert voice.tts_ready(_cfg())[0] is False


def test_edge_tts_is_first_and_gets_the_bulgarian_voice(monkeypatch) -> None:
    seen: list = []
    mod = types.ModuleType("edge_tts")
    mod.__spec__ = importlib.machinery.ModuleSpec("edge_tts", None)

    class Communicate:
        def __init__(self, text, voice_name):
            seen.append((text, voice_name))

        async def stream(self):
            yield {"type": "WordBoundary"}
            yield {"type": "audio", "data": b"ID3mp3-"}
            await asyncio.sleep(0)
            yield {"type": "audio", "data": b"data"}

    mod.Communicate = Communicate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edge_tts", mod)
    assert voice.synthesize("Здравей", cfg=_cfg()) == (b"ID3mp3-data", "audio/mpeg")
    assert voice.synthesize("Пак", "bg-BG-BorislavNeural", cfg=_cfg())[1] == "audio/mpeg"
    assert seen == [("Здравей", "bg-BG-KalinaNeural"), ("Пак", "bg-BG-BorislavNeural")]


def test_edge_tts_failing_offline_falls_back_to_espeak(monkeypatch) -> None:
    mod = types.ModuleType("edge_tts")
    mod.__spec__ = importlib.machinery.ModuleSpec("edge_tts", None)

    class Communicate:
        def __init__(self, *a):
            pass

        async def stream(self):
            raise OSError("Cannot connect to host speech.platform.bing.com")
            yield {}

    mod.Communicate = Communicate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "edge_tts", mod)
    monkeypatch.setattr(voice.shutil, "which", lambda name: "/usr/bin/espeak-ng" if name == "espeak-ng" else None)
    runs: list = []

    def run(argv, input=None, **kw):
        runs.append((argv, input))
        return types.SimpleNamespace(returncode=0, stdout=b"RIFF....WAVE", stderr=b"")

    monkeypatch.setattr(voice.subprocess, "run", run)
    assert voice.synthesize("Здравей, свят", cfg=_cfg()) == (b"RIFF....WAVE", "audio/wav")
    ((argv, sent),) = runs
    # Текстът е по stdin в UTF-8 — нищо на кирилица в командния ред.
    assert argv == ["/usr/bin/espeak-ng", "-v", "bg", "-b", "1", "--stdin", "--stdout"]
    assert sent == "Здравей, свят".encode() and all(a.isascii() for a in argv)


def _fake_pyttsx3(monkeypatch, voices: list) -> list:
    saved: list = []
    mod = types.ModuleType("pyttsx3")
    mod.__spec__ = importlib.machinery.ModuleSpec("pyttsx3", None)

    class Engine:
        def getProperty(self, name):
            return voices

        def setProperty(self, name, value):
            saved.append((name, value))

        def save_to_file(self, text, path):
            saved.append(("path", path))
            with open(path, "wb") as f:
                f.write(b"RIFFsapi")

        def runAndWait(self):
            pass

    mod.init = lambda: Engine()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyttsx3", mod)
    return saved


def test_pyttsx3_without_a_bulgarian_voice_says_so_on_windows(monkeypatch) -> None:
    v = types.SimpleNamespace
    _fake_pyttsx3(monkeypatch, [v(id="HKLM\\...\\TTS_MS_EN-US_ZIRA_11.0", name="Microsoft Zira Desktop",
                                  languages=[])])
    monkeypatch.setattr(voice.sys, "platform", "win32")
    with pytest.raises(VoiceError) as e:
        voice.synthesize("Здравей", cfg=_cfg(tts_backends=("pyttsx3",)))
    assert "няма глас за „bg“" in str(e.value) and "SAPI5" in str(e.value)


def test_pyttsx3_with_a_bulgarian_voice_writes_only_to_a_temp_dir(monkeypatch, tmp_path) -> None:
    import os
    v = types.SimpleNamespace
    saved = _fake_pyttsx3(monkeypatch, [v(id="english", name="English", languages=[b"\x05en"]),
                                        v(id="bulgarian", name="Bulgarian", languages=[b"\x05bg"])])
    assert voice.synthesize("Здравей", cfg=_cfg(tts_backends=("pyttsx3",))) == (b"RIFFsapi", "audio/wav")
    assert ("voice", "bulgarian") in saved
    path = next(p for k, p in saved if k == "path")
    assert not os.path.exists(path)                   # временната папка е изтрита


def test_bulgarian_voice_detection() -> None:
    v = types.SimpleNamespace
    assert voice.bulgarian_voice_id([v(id="TTS_MS_BG-BG_IVAN_11.0", name="Ivan", languages=[])]) \
        == "TTS_MS_BG-BG_IVAN_11.0"
    assert voice.bulgarian_voice_id([v(id="x", name="Microsoft Ivan - Bulgarian", languages=[])]) == "x"
    assert voice.bulgarian_voice_id([v(id="bgreen", name="Big Green", languages=["en_US"])]) is None


# ── пускане ─────────────────────────────────────────────────────────────────

def _which(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


@pytest.mark.parametrize("mime,plat,present,kind,exe", [
    ("audio/wav", "win32", ("ffplay",), "winsound", None),
    ("audio/mpeg", "win32", (), "mci", None),
    ("audio/mpeg", "win32", ("ffplay",), "exe", "ffplay"),
    ("audio/mpeg", "linux", ("ffplay", "mpg123"), "exe", "ffplay"),
    ("audio/wav", "linux", ("paplay", "aplay"), "exe", "paplay"),
    ("audio/wav", "linux", ("aplay",), "exe", "aplay"),
    ("audio/mpeg", "linux", ("mpg123", "aplay"), "exe", "mpg123"),
    ("audio/mpeg", "darwin", ("afplay",), "file", "afplay"),
])
def test_the_player_is_chosen_without_heavy_deps(mime, plat, present, kind, exe) -> None:
    player = voice.choose_player(mime, platform=plat, which=_which(*present))
    assert player is not None and player.kind == kind
    if exe:
        assert player.argv[0] == f"/usr/bin/{exe}"
    if exe == "ffplay":
        assert player.argv[-2:] == ["-i", "pipe:0"]   # звукът по stdin, не файл


def test_no_player_is_a_clear_error(monkeypatch) -> None:
    assert voice.choose_player("audio/mpeg", platform="linux", which=_which("aplay")) is None
    monkeypatch.setattr(voice, "choose_player", lambda mime: None)
    with pytest.raises(VoiceError, match="ffplay"):
        voice.play(b"ID3", "audio/mpeg")


def test_speak_says_only_the_short_clean_version(monkeypatch) -> None:
    said: list = []
    monkeypatch.setattr(voice, "synthesize", lambda text, voice=None, cfg=None: (said.append(text) or b"ID3", "audio/mpeg"))
    played: list = []
    monkeypatch.setattr(voice, "_run_player", lambda player, audio, mime: played.append((audio, mime)))
    monkeypatch.setattr(voice, "choose_player", lambda mime: voice.Player("exe", ["/usr/bin/ffplay"]))
    t = voice.speak("**Готово.** Едно. Две. Три. Четири. Пет.\n```\nкод\n```", block=True,
                    cfg=_cfg(max_sentences=2))
    assert t is not None and not t.is_alive()
    assert said == ["Готово. Едно. Останалото е на екрана."] and played == [(b"ID3", "audio/mpeg")]
    assert voice.speak("```\nсамо код\n```", cfg=_cfg()) is not None   # „Кодът е на екрана.“
    assert voice.speak("", cfg=_cfg()) is None


def test_an_older_answer_arriving_late_is_not_played(monkeypatch) -> None:
    """edge-tts е онлайн: звукът на стария отговор може да дойде след новия."""
    slow = threading.Event()

    def synthesize(text, voice=None, cfg=None):
        if text.startswith("Първи"):
            slow.wait(5)
        return text.encode(), "audio/mpeg"

    monkeypatch.setattr(voice, "synthesize", synthesize)
    played: list[bytes] = []
    monkeypatch.setattr(voice, "_run_player", lambda player, audio, mime: played.append(audio))
    monkeypatch.setattr(voice, "choose_player", lambda mime: voice.Player("exe", ["ffplay"]))
    first = voice.speak("Първи.", cfg=_cfg())
    voice.speak("Втори.", block=True, cfg=_cfg())
    slow.set()
    assert first is not None
    first.join(5)
    assert played == ["Втори.".encode()]


def test_speak_reports_errors_instead_of_raising(monkeypatch) -> None:
    _no_tts_installed(monkeypatch)
    errors: list[str] = []
    voice.speak("Здравей", block=True, cfg=_cfg(), on_error=errors.append)
    assert len(errors) == 1 and voice.INSTALL_HINT in errors[0]


# ── микрофон ────────────────────────────────────────────────────────────────

def _chunk(level: int, seconds: float = 0.1) -> bytes:
    n = int(seconds * voice.SAMPLE_RATE)
    return struct.pack(f"<{n}h", *([level, -level] * (n // 2)))


def test_silence_after_speech_stops_the_recording() -> None:
    d = voice.SilenceDetector(1.0)
    noise = [_chunk(50)] * 3
    assert not any(d.feed(c) for c in noise)                 # шумът в началото — прагът
    assert not any(d.feed(_chunk(5000)) for _ in range(5))   # реч
    stops = [d.feed(_chunk(60)) for _ in range(10)]
    assert stops.index(True) == 9                             # 1.0 s тишина = 10 парчета


def test_silence_before_speech_or_with_detection_off_never_stops() -> None:
    d = voice.SilenceDetector(0.5)
    assert not any(d.feed(_chunk(30)) for _ in range(50))
    off = voice.SilenceDetector(0)
    assert not any(off.feed(c) for c in [_chunk(5000)] * 3 + [_chunk(0)] * 50)


def test_record_returns_16khz_mono_wav_in_memory(monkeypatch) -> None:
    got: list = []

    def capture(seconds, stop, detector):
        got.append((seconds, detector.silence_seconds))
        return _chunk(1000, 0.5)

    monkeypatch.setattr(voice, "_capture", capture)
    wav_bytes = voice.record(max_seconds=500, silence_seconds=0)
    with wave.open(io.BytesIO(wav_bytes)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == 8000
    assert got == [(voice.MAX_AUDIO_SECONDS, 0)]


def test_without_sounddevice_the_error_says_how_to_install(monkeypatch) -> None:
    _no_tts_installed(monkeypatch)
    monkeypatch.setattr(voice.sys, "platform", "win32")
    with pytest.raises(VoiceError) as e:
        voice.record(1)
    assert voice.INSTALL_HINT in str(e.value) and "sounddevice" in str(e.value)


def test_linux_without_portaudio_falls_back_to_arecord(monkeypatch) -> None:
    real = voice.find_spec
    monkeypatch.setattr(voice, "find_spec", lambda n: object() if n == "sounddevice" else real(n))

    def no_portaudio(*a):
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(voice, "_capture_sounddevice", no_portaudio)
    monkeypatch.setattr(voice, "_capture_arecord", lambda s, stop, d: b"\x01\x00" * 16000)
    monkeypatch.setattr(voice.sys, "platform", "linux")
    monkeypatch.setattr(voice.shutil, "which", _which("arecord"))
    assert voice.audio_seconds(voice.record(2), "audio/wav") == 1.0


# ── /listen ─────────────────────────────────────────────────────────────────

class _Keys:
    """Фалшива клавиатура: по време на записа и при въпроса след него."""

    def __init__(self, during: list[str | None], after: list[str]) -> None:
        self.during, self.after = list(during), list(after)

    def __call__(self, timeout):
        if timeout is not None:          # записът — питане с кратко чакане
            return self.during.pop(0) if self.during else None
        return self.after.pop(0)


def _flow(keys: _Keys, *, audio: bytes | None = None, text: str = "пусни тестовете",
          edit=lambda t: t, error: Exception | None = None):
    lines: list[str] = []
    stt_calls: list = []
    stopped = threading.Event()

    def capture(seconds, stop, silence_seconds=None):
        assert stop.wait(5)              # спира се от клавиша
        stopped.set()
        return _wav(1.2) if audio is None else audio

    def stt(a, mime, language):
        stt_calls.append((mime, language))
        if error is not None:
            raise error
        return text

    result = voice.listen_flow(out=lines.append, key=keys, edit=edit, cfg=_cfg(),
                               capture=capture, stt=stt)
    return result, lines, stt_calls, stopped


def test_listen_shows_what_it_heard_and_enter_sends_it() -> None:
    result, lines, stt_calls, stopped = _flow(_Keys([None, "enter"], ["enter"]))
    assert result == "пусни тестовете" and stopped.is_set()
    assert stt_calls == [("audio/wav", "bg")]
    assert f"Чух: пусни тестовете  {voice.CONFIRM_PROMPT}" in lines
    assert voice.CONFIRM_PROMPT == "[Enter = изпрати, e = редактирай, Esc = откажи]"


@pytest.mark.parametrize("key", ["e", "е", "E"])   # латинско, кирилско, главно
def test_listen_e_lets_the_operator_fix_the_text(key) -> None:
    seen: list[str] = []
    result, *_ = _flow(_Keys(["space"], [key]), edit=lambda t: seen.append(t) or "пусни pytest")
    assert seen == ["пусни тестовете"] and result == "пусни pytest"


def test_listen_esc_after_hearing_sends_nothing() -> None:
    result, lines, *_ = _flow(_Keys(["enter"], ["x", "esc"]))   # непознат клавиш — пита пак
    assert result is None and lines[-1] == "Отказано."
    assert sum(1 for line in lines if line.startswith("Чух:")) == 2


def test_listen_esc_while_recording_never_transcribes() -> None:
    result, lines, stt_calls, _ = _flow(_Keys(["esc"], []))
    assert result is None and stt_calls == [] and "Отказано." in lines


def test_listen_too_short_or_misheard_or_failing() -> None:
    result, lines, stt_calls, _ = _flow(_Keys(["enter"], []), audio=_wav(0.1))
    assert result is None and stt_calls == [] and "твърде кратък" in lines[-1]
    result, lines, *_ = _flow(_Keys(["enter"], []), text="  ")
    assert result is None and lines[-1] == "Нищо не разпознах."
    result, lines, *_ = _flow(_Keys(["enter"], []), error=VoiceError("Няма с какво"))
    assert result is None and lines[-1] == "⚠ Няма с какво"


def test_ctrl_c_in_listen_is_a_cancel_not_an_exit_from_the_chat() -> None:
    class Interrupt(_Keys):
        def __call__(self, timeout):
            raise KeyboardInterrupt

    result, lines, stt_calls, stopped = _flow(Interrupt([], []))
    assert result is None and stt_calls == [] and stopped.is_set() and lines[-1] == "Отказано."

    def edit(text):
        raise KeyboardInterrupt

    result, lines, *_ = _flow(_Keys(["enter"], ["e"]), edit=edit)
    assert result is None and lines[-1] == "Отказано."


def test_listen_edit_to_empty_is_a_cancel() -> None:
    result, lines, *_ = _flow(_Keys(["enter"], ["e"]), edit=lambda t: "  ")
    assert result is None and lines[-1] == "Отказано."


def test_listen_without_a_mic_says_why() -> None:
    lines: list[str] = []

    def capture(seconds, stop, silence_seconds=None):
        raise VoiceError("Не мога да записвам — няма sounddevice")

    assert voice.listen_flow(out=lines.append, key=lambda t: None, cfg=_cfg(), capture=capture,
                             stt=lambda *a: pytest.fail("не се пуска")) is None
    assert lines[-1].startswith("⚠ Не мога да записвам")


def test_edit_line_without_readline_keeps_the_text_on_enter(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "readline", None)    # Windows: няма readline
    assert voice.edit_line("чух това", ask=lambda prompt: "") == "чух това"
    assert voice.edit_line("чух това", ask=lambda prompt: " ново ") == "ново"


# ── /doctor ─────────────────────────────────────────────────────────────────

def test_doctor_lines_without_anything_installed_are_skips_not_failures(monkeypatch, calls) -> None:
    sent, _ = calls
    _no_tts_installed(monkeypatch)
    monkeypatch.setattr(voice.sys, "platform", "win32")
    rows = {name: (status, detail) for name, status, detail, _ in voice.doctor_lines()}
    assert set(rows) == {"Глас → текст", "Текст → глас", "Микрофон", "Звук (mp3)"}
    assert rows["Глас → текст"][0] == "skip" and "groq: няма ключ" in rows["Глас → текст"][1]
    assert rows["Текст → глас"][0] == "skip" and rows["Микрофон"] == ("skip", "sounddevice не е инсталиран")
    assert rows["Звук (mp3)"] == ("ok", "Windows MCI (winmm)")
    assert sent == []                                   # без мрежа


def test_doctor_shows_the_groq_keys_by_count_never_by_value(monkeypatch, calls) -> None:
    from pathlib import Path

    from genesis_agent import doctor
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    monkeypatch.setenv("GROQ_API_KEY_2", GROQ_KEY_2)
    checks = doctor.check_voice(Path.cwd())
    stt = next(c for c in checks if c.name == "Глас → текст")
    assert stt.status == "ok" and "groq (2 ключа)" in stt.detail
    assert GROQ_KEY not in repr(checks) and GROQ_KEY_2 not in repr(checks)
    assert ("Глас", doctor.check_voice) in doctor.probes()


def test_doctor_names_the_input_device(monkeypatch) -> None:
    real = voice.find_spec
    monkeypatch.setattr(voice, "find_spec", lambda n: object() if n == "sounddevice" else real(n))
    sd = types.ModuleType("sounddevice")
    sd.query_devices = lambda kind=None: {"name": "Microphone Array (Realtek)"}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    assert voice.mic_status() == ("ok", "„Microphone Array (Realtek)“")

    def none(kind=None):
        raise ValueError("No input device matching")

    sd.query_devices = none  # type: ignore[attr-defined]
    monkeypatch.setattr(voice.sys, "platform", "win32")
    status, detail = voice.mic_status()
    assert status == "fail" and "няма входно устройство" in detail
