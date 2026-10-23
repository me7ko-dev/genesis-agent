"""Одит на прозореца (desktop/), 2026-10-22.

По един тест на находка. Всеки падаше на кода преди поправката. Правилата са
чисти TypeScript функции (без DOM и Electron), затова тестът ги пуска през
`node --experimental-strip-types`, както `npm test` в desktop/. Без Node 22.6+
тестът се пропуска (Python задачите в CI нямат задължително такъв Node).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("няма node")
    probe = subprocess.run([node, "--experimental-strip-types", "-e", "0"],
                           capture_output=True, text=True, timeout=60, check=False)
    if probe.returncode != 0:
        pytest.skip("този node не разбира --experimental-strip-types")
    return node


def _run_ts(tmp_path: Path, code: str) -> dict:
    """Пуска `code` като ES модул и връща JSON-а, който той отпечата."""
    node = _node()
    script = tmp_path / "probe.mts"
    script.write_text(code, encoding="utf-8")
    r = subprocess.run([node, "--experimental-strip-types", "--no-warnings", str(script)],
                       capture_output=True, timeout=120, cwd=str(tmp_path), check=False)
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, err or out
    return json.loads(out.strip().splitlines()[-1])


def _csp() -> dict[str, list[str]]:
    html = (DESKTOP / "src" / "renderer" / "index.html").read_text(encoding="utf-8")
    start = html.index('content="', html.index("Content-Security-Policy")) + len('content="')
    policy = html[start:html.index('"', start)]
    out: dict[str, list[str]] = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


def test_model_markdown_cannot_load_unc_or_remote_resources(tmp_path):
    """Находка 1: `![a](//evil/share/a.png)` и `<img src="\\\\evil\\share\\d.png">`
    в отговора минаваха DOMPurify, а на file:// страницата ставаха
    file://evil/share/... — 'self' в img-src/media-src ги пускаше и Chromium
    ги искаше (под Windows това е UNC: SMB към чужд хост и NTLM хешът)."""
    safe = (DESKTOP / "src" / "renderer" / "lib" / "safeurl.ts").as_uri()
    res = _run_ts(tmp_path, f"""
import {{ resourceAllowed, styleAllowed }} from {json.dumps(safe)};
const bad = [
  ['img', 'src', '//evil.example/share/a.png'],
  ['img', 'src', '\\\\\\\\evil.example\\\\share\\\\d.png'],
  ['img', 'src', '/\\\\evil.example/share/x.png'],
  ['img', 'src', '  //evil.example/x.png'],
  ['img', 'src', 'file://evil.example/share/c.png'],
  ['img', 'src', 'https://evil.example/b.png'],
  ['img', 'src', 'x.png'],
  ['video', 'src', '//evil.example/share/v.mp4'],
  ['video', 'poster', '//evil.example/p.png'],
  ['source', 'srcset', 'data:image/png;base64,AAAA 1x, //evil.example/x.png 2x'],
  ['table', 'background', '//evil.example/t.png'],
  ['image', 'href', '//evil.example/i.png'],
  ['feimage', 'xlink:href', '//evil.example/i.png'],
  ['rect', 'filter', 'url(//evil.example/share/f.svg#f)'],
  ['rect', 'clip-path', 'url("//evil.example/share/c.svg#c")'],
  ['rect', 'mask', 'url(//evil.example/share/m.svg#m)'],
  ['rect', 'fill', 'url(//evil.example/share/p.svg#p) red'],
  ['path', 'marker-start', 'url(//evil.example/share/k.svg#k)'],
];
const good = [
  ['img', 'src', 'data:image/png;base64,iVBORw0KGgo='],
  ['audio', 'src', 'blob:file:///0f1e2d3c'],
  ['a', 'href', 'https://example.com/docs'],
  ['img', 'alt', '//не е адрес'],
  ['rect', 'fill', 'url(#grad)'],
  ['rect', 'fill', 'red'],
];
console.log(JSON.stringify({{
  bad: bad.filter(([t, n, v]) => resourceAllowed(t, n, v)),
  good: good.filter(([t, n, v]) => !resourceAllowed(t, n, v)),
  style: [styleAllowed('color: red'), styleAllowed('background:url(//evil/x.png)'),
          styleAllowed('background: \\\\75 rl(//evil/x.png)'), styleAllowed('background-image: image-set("//e/x" 1x)')],
}}));
""")
    assert res["bad"] == [], "минават към мрежата/файловете"
    assert res["good"] == [], "свалени без нужда"
    assert res["style"] == [True, False, False, False]
    md = (DESKTOP / "src" / "renderer" / "lib" / "markdown.ts").read_text(encoding="utf-8")
    assert "resourceAllowed" in md and "addHook" in md
    # Втората стена: CSP-то не пуска file:// (за file:// страница 'self' е file:).
    csp = _csp()
    assert "'self'" not in csp["img-src"] and "'self'" not in csp["media-src"]
    assert {"data:", "blob:"} <= set(csp["img-src"]) and "blob:" in csp["media-src"]


def test_long_message_error_is_bulgarian_and_names_the_limit(tmp_path):
    """Находка 2: над 20 000 знака прозорецът казваше „Не се изпрати: too_long“,
    а над 256 KiB — „Съобщението с картинките е твърде голямо.“ без картинки."""
    media = (DESKTOP / "src" / "shared" / "media.ts").as_uri()
    res = _run_ts(tmp_path, f"""
import {{ MAX_MESSAGE, messageLength, sendErrorText, lengthHint }} from {json.dumps(media)};
console.log(JSON.stringify({{
  max: MAX_MESSAGE,
  longNoImages: sendErrorText({{ ok: false, error: 'too_long' }}, false),
  largeNoImages: sendErrorText({{ ok: false, error: 'too_large' }}, false),
  largeImages: sendErrorText({{ ok: false, error: 'too_large' }}, true),
  manyImages: sendErrorText({{ ok: false, error: 'too_many_images', max: 4 }}, true),
  other: sendErrorText({{ ok: false, error: 'weird_code' }}, false),
  emoji: messageLength('\\u{{1F600}}'.repeat(3)),
  quiet: lengthHint('a'.repeat(1000)),
  near: lengthHint('a'.repeat(19000)),
  over: lengthHint('a'.repeat(20001)),
}}));
""")
    assert res["max"] == 20000
    assert "20000" in res["longNoImages"] and "too_long" not in res["longNoImages"]
    assert "картин" not in res["largeNoImages"] and "too_large" not in res["largeNoImages"]
    assert "картин" in res["largeImages"]
    assert "4 картинки" in res["manyImages"]
    assert res["other"].startswith("Не се изпрати")
    # Сървърът брои с Python len() — кодови точки, не UTF-16 единици.
    assert res["emoji"] == 3
    assert res.get("quiet") is None   # undefined не влиза в JSON-а
    assert "19000" in res["near"] and "20000" in res["near"]
    assert "20001" in res["over"]
    app = (DESKTOP / "src" / "renderer" / "App.tsx").read_text(encoding="utf-8")
    assert "sendErrorText(" in app and "Не се изпрати: ${r.error}" not in app
    composer = (DESKTOP / "src" / "renderer" / "components" / "Composer.tsx").read_text(encoding="utf-8")
    assert "lengthHint(" in composer
