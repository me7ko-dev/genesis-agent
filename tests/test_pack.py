"""genesis_agent.pack — the project as the client gets it (NEXT_STEPS Д.13)."""
import zipfile

from genesis_agent import pack


def _project(tmp_path):
    root = tmp_path / "guesthouse"
    for rel, data in {
        "app.py": "print('hi')\n",
        "README.md": "# Къща за гости\n",
        "static/logo.png": "PNG",
        "tests/test_app.py": "def test_ok():\n    pass\n",
        ".env": "OPENROUTER_API_KEY=sk-secret\n",
        "keys/id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\n",
        ".venv/pyvenv.cfg": "home = /usr/bin\n",
        ".git/config": "",
        "__pycache__/app.cpython-312.pyc": "",
        "tests/__pycache__/t.pyc": "",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8")
    return root


def test_zip_has_the_work_and_never_the_keys_or_the_tooling(tmp_path):
    root = _project(tmp_path)
    res = pack.pack(root, run_tests=False)
    assert res.zip_path.parent == tmp_path and res.zip_path.suffix == ".zip"
    names = set(zipfile.ZipFile(res.zip_path).namelist())
    assert {"guesthouse/app.py", "guesthouse/README.md", "guesthouse/static/logo.png",
            "guesthouse/tests/test_app.py"} <= names
    assert not any(".env" in n or "id_rsa" in n or ".venv" in n or ".git/" in n
                   or "__pycache__" in n for n in names), names
    assert sorted(res.secrets) == [".env", "keys/id_rsa"]


def test_a_project_inside_a_skipped_name_is_still_packed(tmp_path):
    """Само частите ПОД проекта решават: проект в …/build/site не е празен."""
    root = _project(tmp_path / "build")
    names = zipfile.ZipFile(pack.pack(root, run_tests=False).zip_path).namelist()
    assert "guesthouse/app.py" in names


def test_the_zip_never_packs_itself(tmp_path):
    root = _project(tmp_path)
    res = pack.pack(root, out=root / "dist.zip", run_tests=False)
    assert not any(n.endswith("dist.zip") for n in zipfile.ZipFile(res.zip_path).namelist())


def _report(res):
    return zipfile.ZipFile(res.zip_path).read(f"guesthouse/{pack.REPORT}").decode("utf-8")


def test_report_runs_the_tests_and_says_how_to_run_without_this_machines_paths(tmp_path):
    root = _project(tmp_path)
    (root / "requirements.txt").write_text("flask\n", encoding="utf-8")
    res = pack.pack(root)
    report = _report(res)
    assert res.tests.startswith("✅") and "1 passed" in res.tests
    assert "`pip install -r requirements.txt`" in report and "`python app.py`" in report
    assert "тестове: `python -m pytest -q`" in report
    assert "- .env" in report and "- keys/id_rsa" in report and "README.md" in report


def test_report_says_when_tests_fail(tmp_path):
    root = _project(tmp_path)
    (root / "tests/test_app.py").write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    res = pack.pack(root)
    assert res.tests.startswith("❌") and "1 failed" in res.tests
    assert res.tests in _report(res)


def test_own_file_named_like_the_report_is_not_packed_twice(tmp_path):
    root = _project(tmp_path)
    (root / pack.REPORT).write_text("old\n", encoding="utf-8")
    names = zipfile.ZipFile(pack.pack(root, run_tests=False).zip_path).namelist()
    assert names.count(f"guesthouse/{pack.REPORT}") == 1


def test_a_package_main_is_run_as_a_module(tmp_path):
    """`python faktura/__main__.py` гърми на `from .extract import …` (2026-10-02, faktura-excel)."""
    root = _project(tmp_path)
    (root / "app.py").unlink()
    for rel in ("faktura/__init__.py", "faktura/__main__.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("", encoding="utf-8")
    report = _report(pack.pack(root, run_tests=False))
    assert "`python -m faktura`" in report and "__main__.py`" not in report


def test_what_the_projects_gitignore_leaves_out_the_client_does_not_get(tmp_path):
    """Логове и изходи от пусканията (genesis_run.log с пътищата на тази машина)."""
    root = _project(tmp_path)
    (root / ".gitignore").write_text(
        "# ours\n*.log\n!keep.log\noutput.xlsx\nbuild/\n/docs/draft.md\n/notes.txt\n.env\n", encoding="utf-8")
    for rel in ("genesis_run.log", "keep.log", "output.xlsx", "build/x.txt", "sub/build",
                "docs/draft.md", "src/docs/draft.md", "notes.txt", "sub/notes.txt"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x", encoding="utf-8")
    res = pack.pack(root, run_tests=False)
    names = set(zipfile.ZipFile(res.zip_path).namelist())
    assert {"guesthouse/keep.log", "guesthouse/sub/build", "guesthouse/src/docs/draft.md",
            "guesthouse/sub/notes.txt", "guesthouse/.gitignore", "guesthouse/app.py"} <= names
    assert not {"guesthouse/genesis_run.log", "guesthouse/output.xlsx", "guesthouse/build/x.txt",
                "guesthouse/docs/draft.md", "guesthouse/notes.txt"} & names
    assert ".env" in res.secrets  # a key the author ignores is still named for the client


def test_client_command_drops_a_quoted_windows_path():
    assert pack._client_command('"C:/Program Files/Py/python.exe" -m pytest -q') == "python -m pytest -q"
    assert pack._client_command("/home/u/p/.venv/bin/python -m pytest -q") == "python -m pytest -q"
    assert pack._client_command("npm test") == "npm test"
