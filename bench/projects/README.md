# Проекти със скрити приемни тестове

Мерят дали Genesis **създава** работещ проект, не дали минава собствените си
тестове (те са зелени почти винаги — и когато резултатът е грешен).

Всяка папка: `task.txt` — задачата, както я пише операторът; `test_hidden.py` —
приемните тестове. Genesis никога не вижда `test_hidden.py`.

| Проект | Какво проверява | 2026-09-25 |
|---|---|---|
| `egn-check` | ЕГН: контролна цифра, векове, пол | 0/6 → 3/3 с умението `bg_egn_*` |
| `eik-check` | ЕИК/БУЛСТАТ 9 и 13 цифри, резервни тегла | 0/2 → 2/2 с умение |
| `euro-convert` | лв → € (1.95583), евро от 2026-01-01 | 0/2 → 2/2 с умение |
| `iban-check` | IBAN BG: 22 знака, BIC код, mod 97 (Наредба № 13 на БНБ) | 0/2 → 2/2 с умение |
| `vat-check` | ДДС №: BG + 9-цифрен ЕИК или ЕГН (не 13-цифрен) | 0/2 → 2/2 с умение |
| `workdays` | работни дни по чл. 154 КТ: Великден, прехвърляне от уикенда, 1.11 | 0/2 → 2/2 с умение |
| `sales-report` | CSV от каса: `;`, десетична запетая, развалени редове, BOM | 2/2 |
| `tasks-api` | Flask + SQLite REST API, статуси, запазване след рестарт | 2/2 |
| `fuel-prices` | HTML таблица, „2,59 лв.“, „-“, чужда таблица преди нея | 2/2 |
| `faktura-excel` | реалистична българска PDF фактура (само Windows — шрифт Arial) | ✅ след `genesis fix` |

По-трудните (2026-10-01, типът поръчки онлайн — план А.2 в NEXT_STEPS.md):

| Проект | Какво проверява |
|---|---|
| `clients-migrate` | CSV → SQLite: BOM, имейл с главни букви = един клиент, +359, ДД.ММ.ГГГГ, второ пускане без дубликати |
| `shop-scraper` | страници през `rel="next"`: относителни адреси, цикъл, дубликати, „1 299,00 лв.“ → €, старата цена |
| `todo-cli` | argparse + флаг > среда > `todo.toml` > подразбиране, кодове за изход, stderr |
| `booking-form` | Flask форма: 303, стойностите остават при грешка, застъпване на дати, екраниране, `<label for>` |
| `expense-bot` | Telegram бот без мрежа: месец по Europe/Sofia, отделни чатове, `/sum@бот`, ⚠️ при грешка |
| `sklad` | пакет от 5+ модула: FIFO себестойност, нищо не се променя при липса, JSON след рестарт, CLI |

## Пускане на всички наведнъж

```powershell
# python-ът на pipx venv-а има зависимостите на Genesis; кодът е от това репо
& "$HOME\AppData\Local\pipx\pipx\venvs\genesis-agent\Scripts\python.exe" scripts\bench_projects.py --runs 2
# само някои / сравнение с предишно пускане
... scripts\bench_projects.py --only egn-check,workdays --compare $HOME\.genesis\bench\<дата>\results.json
```

Всеки пуск е в празна папка; `test_hidden.py` се копира извън нея чак след
края. Таблицата накрая: изцяло верни пускове, дял скрити тестове, секунди,
токени. Логовете и `results.json` остават в папката от `--out`.

## Пускане на един проект на ръка

Зависимостите на проекта (pytest, flask, beautifulsoup4, pdfplumber, openpyxl,
reportlab) трябва да са в системния Python — Genesis ги ползва за тестовете.

```bash
mkdir -p /tmp/trial && cd /tmp/trial
{ cat <repo>/bench/projects/egn-check/task.txt; printf 'изход\n'; } | genesis > run.log 2>&1
cp <repo>/bench/projects/egn-check/test_hidden.py /tmp/hidden/
PYTHONPATH=/tmp/trial python -m pytest -q --rootdir /tmp/hidden /tmp/hidden/test_hidden.py
```

Един пуск не е присъда — моделите във веригата се сменят. Мери се поне 2–3
пъти на проект и се гледа и времето (`run.log`: кой модел е отговорил, `✗` —
защо е отпаднал).
