# Handoff — /update self-update feature (2026-09-22)

**Клон**: `claude/token-upgrade-ipe4yg` · **PR**: #1 · **връх**: `7d3699f` · CI зелено (Linux+Windows), mergeable.

## Какво е направено
- `/update` команда в чата — проверява GitHub, показва changelog (commit subjects между инсталирания и новия комит) и стар→нов комит, пита за потвърждение, обновява на заден план след излизане от процеса (`genesis_agent/self_update.py`, `version_info.py`).
- `genesis update` (CLI) — същата проверка, само показва.
- Нова `genesis budget [N]` команда — cache read/write статистика от `budget_log.jsonl`.
- Още фиксове на клона (от други сесии): `search_code` не пропускаше `.github/`, `/model` Gemini вече минава през Brain, defensive parsing при чупен GitHub отговор.
- Тестове: 1466 passed, 5 skipped · ruff чисто · mypy чисто (вкл. `--platform win32`).

## Какво предстои
- Тест на `/update` на живо Windows лаптоп (единственото непроверено — заключване на `.exe` докато тече, mock-нато в тестовете, не реално).
- Ако мине — PR #1 е готов за merge в main.
