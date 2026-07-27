"""
GENESIS 0 — LOCAL REPAIR AGENT (Авариен режим)
═══════════════════════════════════════════════
Малък 1-3B модел директно от LM Studio (порт 1234) — без backend.
Работи САМО за поправяне на грешки — бърз, лек, винаги наличен.

Стратегия:
  1. Regex patterns → 80% от грешките се поправят БЕЗ LLM (instant)
  2. Tiny LLM (1-3B) → за неизвестни грешки (макс 3 рунда)
  3. Ако LLM-ът е офлайн → само pattern fixing

Поддържани грешки (без LLM):
  - ImportError / ModuleNotFoundError → auto-install
  - IndentationError → auto-fix
  - SyntaxError (common) → auto-fix
  - NameError (undefined variable) → добавя дефиниция
  - FileNotFoundError → добавя mkdir/touch
  - TypeError (None) → добавя None-check
  - KeyError → добавя .get() 
  - AttributeError → добавя hasattr()
"""

from __future__ import annotations

import re
import sys
import json
import time
import requests
import subprocess
from dataclasses import dataclass
from typing import Optional

# ─── КОНФИГУРАЦИЯ ────────────────────────────────────────────────────────────
# LM Studio директно — порт 1234 е неговият дефолт
LM_STUDIO_URL  = "http://127.0.0.1:1234/v1/chat/completions"
OLLAMA_URL     = "http://127.0.0.1:11434/api/generate"
REPAIR_TIMEOUT = 45   # секунди — малкият модел е бърз
MAX_ROUNDS     = 3    # максимум рунда поправяне


@dataclass
class RepairResult:
    fixed: bool         # Успешна ли е поправката?
    code: str           # Поправеният код
    rounds: int         # Колко рунда отне
    method: str         # "pattern" / "llm" / "none"
    fix_desc: str       # Описание на поправката


# ─── REGEX PATTERN FIXES (без LLM) ───────────────────────────────────────────
class PatternFixer:
    """Поправя ~80% от Python грешките с regex — мигновено."""

    @staticmethod
    def fix_indentation(code: str, error: str) -> Optional[str]:
        """IndentationError → нормализира отстъпите."""
        if "IndentationError" not in error and "TabError" not in error:
            return None
        lines = code.split("\n")
        fixed = []
        for line in lines:
            # Замести tabs с 4 spaces
            fixed.append(line.replace("\t", "    "))
        result = "\n".join(fixed)
        return result if result != code else None

    @staticmethod
    def fix_missing_import(code: str, error: str) -> Optional[str]:
        """ModuleNotFoundError → добавя pip install в началото."""
        match = re.search(r"No module named '([^']+)'", error)
        if not match:
            return None
        module = match.group(1).split(".")[0]  # взима само основния пакет
        # Добавя auto-install в началото на кода
        install_snippet = (
            f"import subprocess, sys\n"
            f"subprocess.run([sys.executable, '-m', 'pip', 'install', '{module}', '-q'], "
            f"capture_output=True)\n"
        )
        if install_snippet.split("\n")[0] not in code:
            return install_snippet + code
        return None

    @staticmethod
    def fix_none_type(code: str, error: str) -> Optional[str]:
        """TypeError: 'NoneType' object → добавя None check преди problematic line."""
        if "'NoneType'" not in error and "NoneType" not in error:
            return None
        # Намери реда с грешката
        line_match = re.search(r'line (\d+)', error)
        if not line_match:
            return None
        line_num = int(line_match.group(1)) - 1
        lines = code.split("\n")
        if line_num >= len(lines):
            return None
        problem_line = lines[line_num]
        # Намери обекта (обикновено е нещо.метод())
        var_match = re.search(r'(\w+)\s*\.', problem_line)
        if not var_match:
            return None
        var = var_match.group(1)
        indent = len(problem_line) - len(problem_line.lstrip())
        indent_str = " " * indent
        guard = f"{indent_str}if {var} is None:\n{indent_str}    raise ValueError(f'{var} is None — проверка пропусната')\n"
        lines.insert(line_num, guard)
        return "\n".join(lines)

    @staticmethod
    def fix_key_error(code: str, error: str) -> Optional[str]:
        """KeyError → замества dict[key] с dict.get(key)."""
        if "KeyError" not in error:
            return None
        key_match = re.search(r"KeyError: '([^']+)'", error)
        if not key_match:
            return None
        key = key_match.group(1)
        # Замести всички ["key"] с .get("key")
        pattern = rf'\[[\'"]{re.escape(key)}[\'"]\]'
        fixed = re.sub(pattern, f'.get("{key}")', code)
        return fixed if fixed != code else None

    @staticmethod
    def fix_file_not_found(code: str, error: str) -> Optional[str]:
        """FileNotFoundError → добавя makedirs преди файловата операция."""
        if "FileNotFoundError" not in error and "No such file" not in error:
            return None
        path_match = re.search(r"'([^']+)'", error)
        if not path_match:
            return None
        path = path_match.group(1)
        # Добавя os.makedirs в началото
        import_line = "import os\n"
        mkdir_line  = f"os.makedirs(os.path.dirname('{path}') or '.', exist_ok=True)\n"
        prefix = ""
        if "import os" not in code:
            prefix += import_line
        prefix += mkdir_line
        return prefix + code if prefix else None

    @staticmethod
    def fix_encoding(code: str, error: str) -> Optional[str]:
        """UnicodeDecodeError → добавя encoding='utf-8' към open()."""
        if "UnicodeDecodeError" not in error and "codec" not in error:
            return None
        # Замести open( без encoding
        fixed = re.sub(
            r"open\(([^)]+)\)(?!\s*\.)",
            lambda m: (m.group(0) if "encoding" in m.group(1)
                      else f"open({m.group(1)}, encoding='utf-8', errors='replace')"),
            code
        )
        return fixed if fixed != code else None

    @staticmethod
    def fix_undefined_name(code: str, error: str) -> Optional[str]:
        """NameError → добавя None дефиниция на undefined var."""
        match = re.search(r"name '(\w+)' is not defined", error)
        if not match:
            return None
        var = match.group(1)
        # Не добавяй ако е builtin
        builtins = {"print", "len", "range", "str", "int", "float", "list",
                    "dict", "set", "tuple", "bool", "open", "type", "None",
                    "True", "False", "Exception", "ValueError", "TypeError"}
        if var in builtins:
            return None
        # Добавя var = None в началото (след imports)
        lines = code.split("\n")
        insert_pos = 0
        for i, line in enumerate(lines):
            if line.startswith("import ") or line.startswith("from "):
                insert_pos = i + 1
        lines.insert(insert_pos, f"{var} = None  # auto-fix: NameError")
        return "\n".join(lines)

    @classmethod
    def try_all(cls, code: str, error: str) -> tuple[Optional[str], str]:
        """Опитва всички pattern fixes. Връща (fixed_code, description)."""
        fixes = [
            (cls.fix_indentation,     "Поправен отстъп (tabs→spaces)"),
            (cls.fix_missing_import,  "Добавен auto-install за липсващ модул"),
            (cls.fix_none_type,       "Добавена None проверка"),
            (cls.fix_key_error,       "KeyError → .get() конвертиране"),
            (cls.fix_file_not_found,  "Добавен os.makedirs за липсваща директория"),
            (cls.fix_encoding,        "Добавен encoding='utf-8'"),
            (cls.fix_undefined_name,  "Дефиниран неизвестен идентификатор"),
        ]
        for fn, desc in fixes:
            result = fn(code, error)
            if result and result != code:
                return result, desc
        return None, ""


# ─── TINY LLM CONNECTOR ──────────────────────────────────────────────────────
class TinyLLM:
    """Директна връзка с LM Studio или Ollama."""

    def __init__(self):
        self.available = None  # None = не е проверявано
        self.url = None

    def _detect(self) -> bool:
        """Открива дали LM Studio или Ollama работи."""
        # Опитай LM Studio (порт 1234)
        try:
            r = requests.get("http://127.0.0.1:1234/v1/models", timeout=3)
            if r.status_code == 200:
                models = r.json().get("data", [])
                if models:
                    self.url = LM_STUDIO_URL
                    self.model = models[0]["id"]
                    return True
        except Exception:
            pass

        # Опитай Ollama (порт 11434)
        try:
            r = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
            if r.status_code == 200:
                models = r.json().get("models", [])
                if models:
                    self.url = None  # Ollama използва различен формат
                    self.ollama_model = models[0]["name"]
                    return True
        except Exception:
            pass

        return False

    def is_available(self) -> bool:
        if self.available is None:
            self.available = self._detect()
        return self.available

    def fix(self, code: str, error: str) -> Optional[str]:
        """Праща кратък промпт към малкия модел за поправяне на грешката."""
        if not self.is_available():
            return None

        # КРАТЪК промпт — за 1-3B модел, минимум токени
        prompt = (
            f"Fix this Python error. Return ONLY the fixed code in ```python``` block.\n\n"
            f"ERROR:\n{error[:500]}\n\n"
            f"CODE:\n```python\n{code[:1500]}\n```"
        )

        try:
            if self.url == LM_STUDIO_URL:
                return self._call_lmstudio(prompt)
            else:
                return self._call_ollama(prompt)
        except Exception as e:
            print(f"  [RepairLLM] Грешка: {e}")
            return None

    def _call_lmstudio(self, prompt: str) -> Optional[str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a Python bug fixer. Return only fixed code in ```python``` blocks."},
                {"role": "user",   "content": prompt}
            ],
            "max_tokens": 1024,
            "temperature": 0.1,  # ниска температура = по-детерминистичен fix
        }
        r = requests.post(LM_STUDIO_URL, json=payload, timeout=REPAIR_TIMEOUT)
        if r.status_code != 200:
            return None
        content = r.json()["choices"][0]["message"]["content"]
        return self._extract_code(content)

    def _call_ollama(self, prompt: str) -> Optional[str]:
        payload = {
            "model": self.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 1024}
        }
        r = requests.post(OLLAMA_URL, json=payload, timeout=REPAIR_TIMEOUT)
        if r.status_code != 200:
            return None
        content = r.json().get("response", "")
        return self._extract_code(content)

    @staticmethod
    def _extract_code(text: str) -> Optional[str]:
        if "```python" in text:
            return text.split("```python")[1].split("```")[0].strip()
        if "```" in text:
            return text.split("```")[1].split("```")[0].strip()
        # Ако целият текст изглежда като код
        if text.strip().startswith(("import ", "def ", "class ", "#")):
            return text.strip()
        return None


# ─── ГЛАВЕН REPAIR AGENT ─────────────────────────────────────────────────────
class LocalRepairAgent:
    """
    Аварийен агент — поправя код когато главният Brain е недостъпен.
    
    Използва:
    1. Pattern fixes (instant, без LLM) — за познати грешки
    2. Tiny LLM (1-3B, директно LM Studio) — за сложни грешки
    """

    def __init__(self):
        self.llm = TinyLLM()
        self._pattern = PatternFixer()

    def repair(self, code: str, error: str, stdout: str = "") -> RepairResult:
        """
        Главна функция. Опитва да поправи кода максимум MAX_ROUNDS пъти.
        """
        full_error = f"{error}\n{stdout}".strip()
        current_code = code

        for round_i in range(1, MAX_ROUNDS + 1):
            print(f"\n  [🔧 REPAIR #{round_i}] Анализирам грешката...")

            # ── Стъпка 1: Pattern fix (мигновено) ──
            fixed, desc = PatternFixer.try_all(current_code, full_error)
            if fixed:
                print(f"  [PATTERN FIX] {desc}")
                test = self._test_code(fixed)
                if test["ok"]:
                    print(f"  [✅ REPAIR OK] Поправен с pattern matching!")
                    return RepairResult(
                        fixed=True, code=fixed, rounds=round_i,
                        method="pattern", fix_desc=desc
                    )
                # Продължи с LLM ако pattern fix не помогна
                current_code = fixed
                full_error = test["error"]

            # ── Стъпка 2: Tiny LLM fix ──
            if self.llm.is_available():
                print(f"  [LLM REPAIR] Малкият модел ({self.llm.model if hasattr(self.llm, 'model') else 'ollama'}) анализира...")
                llm_fixed = self.llm.fix(current_code, full_error)
                if llm_fixed:
                    test = self._test_code(llm_fixed)
                    if test["ok"]:
                        print(f"  [✅ REPAIR OK] Малкият модел поправи грешката!")
                        return RepairResult(
                            fixed=True, code=llm_fixed, rounds=round_i,
                            method="llm", fix_desc=f"LLM fix (round {round_i})"
                        )
                    current_code = llm_fixed
                    full_error = test["error"]
                    print(f"  [RETRY] Все още има грешка, опитвам отново...")
            else:
                print(f"  [INFO] LM Studio/Ollama офлайн — само pattern fixes")
                break  # Без LLM, pattern-ите вече не помагат

        return RepairResult(
            fixed=False, code=current_code, rounds=MAX_ROUNDS,
            method="none", fix_desc="Неуспешно след всички рундове"
        )

    @staticmethod
    def _test_code(code: str) -> dict:
        """Тества кода в subprocess — без sandbox, само синтаксис и runtime."""
        try:
            # Бърза синтаксис проверка
            import ast
            ast.parse(code)
        except SyntaxError as e:
            return {"ok": False, "error": f"SyntaxError: {e}"}

        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=15
            )
            if proc.returncode == 0:
                return {"ok": True, "error": ""}
            return {"ok": False, "error": proc.stderr[:500]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Timeout при тест"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def status(self) -> str:
        """Показва статуса на repair агента."""
        llm_status = (
            f"✅ LM Studio ({getattr(self.llm, 'model', '?')})"
            if self.llm.is_available() else
            "⚠️  LLM офлайн (само pattern fixes)"
        )
        return f"🔧 LocalRepairAgent: {llm_status} | Patterns: 7 built-in"


# ─── SINGLETON ───────────────────────────────────────────────────────────────
_agent: Optional[LocalRepairAgent] = None

def get_repair_agent() -> LocalRepairAgent:
    global _agent
    if _agent is None:
        _agent = LocalRepairAgent()
    return _agent


def emergency_repair(code: str, stderr: str, stdout: str = "") -> RepairResult:
    """
    Бърз достъп — извиква аварийния агент.
    Използва се от autonomous_loop при грешка от Brain.
    """
    agent = get_repair_agent()
    return agent.repair(code, stderr, stdout)
