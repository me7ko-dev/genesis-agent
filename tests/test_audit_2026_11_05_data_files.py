"""2026-11-05: счупен JSON/YAML/TOML се казва още в резултата на WRITE_FILE/EDIT_FILE.

Проба: `package.json` със запетая накрая, `docker-compose.yml` с изместен отстъп и
`pyproject.toml` с `name = x` минаваха с „✓“ (3 от 3), а грешката излизаше чак при
`npm install` / `docker compose up` / `pip install .`.
"""
from __future__ import annotations

import pytest

import genesis_skills as gs
from genesis_agent.code_validate import data_error


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "_WORKSPACE", tmp_path)
    gs._SEEN_PATHS.clear()
    return tmp_path


_BROKEN = {
    "package.json": '{\n  "name": "x",\n  "version": "1.0.0",\n}\n',
    "docker-compose.yml": "services:\n  web:\n    image: nginx\n   ports:\n      - 80:80\n",
    "config.yaml": "a: [1, 2\nb: 3\n",
}


@pytest.mark.parametrize("name", sorted(_BROKEN))
def test_a_broken_data_file_is_reported_with_the_line(ws, name) -> None:
    out = gs._tool_write_file(name, _BROKEN[name])
    assert "✓" in out and "не е валиден" in out and "ред " in out, out
    assert (ws / name).read_text(encoding="utf-8") == _BROKEN[name]     # записът не се спира


def test_a_broken_toml_is_reported(ws) -> None:
    pytest.importorskip("tomllib" if __import__("sys").version_info >= (3, 11) else "tomli")
    out = gs._tool_write_file("pyproject.toml", "[project]\nname = x\n")
    assert "не е валиден TOML" in out, out


@pytest.mark.parametrize("name, content", [
    ("package.json", '{"name": "x", "version": "1.0.0"}\n'),
    ("tsconfig.json", '{\n  // коментар\n  "compilerOptions": {},\n}\n'),          # JSONC
    (".vscode/settings.json", '{\n  // коментар\n  "a": 1,\n}\n'),
    ("template.yaml", ("Resources:\n  B:\n    Type: AWS::S3::Bucket\n    Properties:\n"
                       "      BucketName: !Sub '${AWS::StackName}-b'\n")),           # CloudFormation
    ("chart/templates/dep.yaml", "metadata:\n  name: {{ .Release.Name }}\n"),       # Helm
    ("k8s.yaml", "a: 1\n---\nb: 2\n"),
    ("empty.json", ""),
    ("pyproject.toml", '[project]\nname = "x"\n'),
])
def test_valid_templated_or_commented_files_get_no_note(ws, name, content) -> None:
    (ws / name).parent.mkdir(parents=True, exist_ok=True)
    out = gs._tool_write_file(name, content)
    assert "не е валиден" not in out, out


def test_an_edit_that_breaks_json_is_reported(ws) -> None:
    (ws / "package.json").write_text('{\n  "name": "x",\n  "private": true\n}\n', encoding="utf-8")
    gs._tool_read_file("package.json")
    out = gs._tool_edit_file("package.json", '"private": true', '"private": true,')
    assert "не е валиден JSON" in out, out


def test_data_error_ignores_other_files(tmp_path) -> None:
    assert data_error(tmp_path / "notes.txt", "{не е json") == ""
    assert data_error(tmp_path / "app.js", "{,}") == ""
