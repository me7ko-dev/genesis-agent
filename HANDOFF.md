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

## Непроверено / потенциално фиктивно (само каквото реално видях в тази сесия, не пълен одит)
- `_pid_alive_win32` (self_update.py) — истинска WinAPI, тестван само с `skipif` guard; истинското заключване на .exe докато процесът тече не е симулирано никъде, само логика.
- Vertex AI секцията в `docs/WINDOWS.md` (OAuth през `gcloud auth application-default login`) — описана, но не изпробвана с реални ключове в тази среда.
- `genesis models --refresh` и `scripts/capability_report.py` — не са пускани с реални API ключове тук (отбелязано в test plan на PR-а, не в самия код).
- Sandbox rlimits по-слаби на Windows (`setrlimit` е POSIX-специфично) — документирано ограничение, не бъг, но означава по-слаба защита там.
- `search_code`/`grep` в тази сесия: 0 TODO/FIXME/XXX маркери в кода — репото изглежда чисто от изоставени бележки (положително, не фиктивно).

## Какво още може да се ъпгрейдне
- Добавяне на Vertex AI като опция в чат-менюто `/model` (потвърдено отсъстващо — grep не намери "vertex" в `genesis_terminal_agent.py`); в момента Vertex се конфигурира само през env vars, не през менюто.
- Истинска WinAPI+file-lock проверка на `/update` на жива Windows машина (виж горе).
