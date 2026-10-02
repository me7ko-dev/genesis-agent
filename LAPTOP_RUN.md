# Пускане на лаптопа — клон `claude/token-protocol` (2026-10-02)

Всичко е в PowerShell, от папката на репото. Пусни стъпките по ред; всяка
пише резултатите си в `~\.genesis\bench\…`. **Върни ми само таблиците накрая
на всяка стъпка** (и `❌` редовете) — не целите логове.

## 0. Подготовка (~3 мин)

```powershell
cd $HOME\Projects\genesis-agent          # ← твоята папка с репото
git fetch origin claude/token-protocol
git checkout claude/token-protocol
git pull origin claude/token-protocol

# Python-ът на pipx (има зависимостите на Genesis; кодът се взима от репото)
$PY = "$HOME\AppData\Local\pipx\pipx\venvs\genesis-agent\Scripts\python.exe"

# Системният Python — с него вървят скритите тестове на bench-а
$env:PYTHONPATH = (Get-Location).Path
$SYS = & $PY -c "from genesis_agent.paths import project_python; print(project_python())"
Remove-Item Env:PYTHONPATH
$SYS                                      # трябва да е истински python.exe, не WindowsApps

& $SYS -m pip install -q pytest flask beautifulsoup4 requests   # за новите bench проекти
pipx inject genesis-agent pytest                                # bench_fix ползва pytest от $PY

$B = "$HOME\.genesis\bench"
```

## 1. `genesis pack` на Windows (~1 мин)

Новата команда — zip за клиента. Пробвай я на истински проект:

```powershell
& $PY -m genesis_agent.cli pack $HOME\Projects\faktura-excel
```

Очаквано: `📦 …\faktura-excel-20261002.zip`, ред `Тестове: ✅ …` и, ако има
`.env`/ключове, ред „Не са включени“. Отвори zip-а: вътре е `ОТЧЕТ.md`.
Върни ми трите реда.

## 2. `bench_fix` — всички 13, вкл. новия `venv_dep` (~3–5 мин)

```powershell
& $PY scripts\bench_fix.py
```

Мярка: `Поправени: 13/13`. Новият `venv_dep` проверява, че тестовете вървят
във `.venv` на проекта (регресия за PR #30). Върни ми последния ред и всеки `❌`.

## 3. `bench_projects` — всички 15, без новия флаг (~75 мин)

Мери две неща наведнъж: новата подкана на code_check срещу 09-30 (старите
10 проекта) и първото пускане на 5-те нови проекта.

```powershell
& $PY scripts\bench_projects.py --runs 2 --test-python $SYS `
    --compare "$B\2026-09-30-history\results.json" --out "$B\2026-10-02-all"
```

Мярка: старите 10 — пак 20/20 (не по-лошо); новите 5 — първа стойност.
Върни ми таблицата накрая.

## 4. `bench_projects` с приемните тестове (Б.4) (~80 мин)

Същото, но с независимите приемни тестове. Сравнява се със стъпка 3.

```powershell
$env:GENESIS_ACCEPTANCE = "1"
& $PY scripts\bench_projects.py --runs 2 --test-python $SYS `
    --compare "$B\2026-10-02-all\results.json" --out "$B\2026-10-02-acceptance"
Remove-Item Env:GENESIS_ACCEPTANCE
```

Решение: повече изцяло верни пускове → флагът става по подразбиране; същото
или по-малко → acceptance.py се маха (струва +1 обръщение на задача).
Върни ми таблицата.

## 5. fcc hard — 30-те трудни, 2 пуска (~60 мин)

Проверява подканата „разминаване → първо очакваното срещу условието“ (c333,
c125). Задачите се взимат от предишното пускане:

```powershell
$old = "$B\fcc-2026-09-30-hard-history\results.json"
$ids = ((Get-Content $old -Raw | ConvertFrom-Json).runs |
        ForEach-Object { [int]$_.project.Substring(1) } | Sort-Object -Unique) -join ","
& $PY scripts\bench_fcc.py --only $ids --runs 2 --compare $old --out "$B\fcc-2026-10-02-hard"
```

Мярка: преди 29/30 (1 пуск). Върни ми таблицата.

## Ако нещо гръмне

- `No module named …` в скрит тест → липсва пакет в `$SYS` (стъпка 0).
- `No module named 'pytest'` в bench_fix → `pipx inject genesis-agent pytest`.
- Висне ли пуск над `--timeout` (900 s) — bench-ът сам го спира; продължава.
- Стъпка 5 (fcc) пази `results.json` след всеки пуск. Стъпки 3–4 го пишат чак
  накрая — не ги прекъсвай; ако няма време наведнъж, цепи с `--only` (напр.
  първо `--only csv-sqlite,cli-config,shop-scraper,contact-form,sklad-package`).
