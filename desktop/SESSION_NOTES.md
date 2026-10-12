# Genesis Desktop: докъде сме стигнали

Последно обновено: 2026-10-12

## Какво е това
Настолно приложение за Windows (Electron + React + Vite) около Genesis агента,
в стила на Claude Code. Приложението пуска `genesis.exe serve` на 127.0.0.1 и
говори с него по криптирания протокол на `remote_server.py` (същия като на мобилното приложение).

## Къде се намира
- Worktree: `C:\Users\roika\Projects\genesis-desktop`, клон **`feat/desktop-oct12`**
  (= старият `feat/desktop-app` + merge на `origin/main` 0365470 след PR #59; качен в origin)
- **PR #60** към main: https://github.com/me7ko-dev/genesis-agent/pull/60 — чака „да“ от потребителя
- Основното репо: `C:\Users\roika\Projects\genesis-agent` (GitHub: me7ko-dev/genesis-agent)
- Кодът на приложението: `desktop/`

## 2026-10-12: върху новия Genesis (мега-ъпгрейдът, PR #59)
Потребителят поиска приложението да „работи с новата версия на Genesis“.
- Merge на main в клона: конфликтите в `remote_server.py` са разрешени така, че
  остават и `--bind` + операцията `command`, и новото от main (`--web`,
  `replay_path`, `reset_usage()`, `unknown_op` вместо ProtocolError).
  `ServeOptions` има `web`. Тестът за непозната операция е сверен.
- Клиентът (`backend.ts`) говори протокола от 2026-10-11: `events` с `more`
  (дотегля веднага), нов `epoch` → от 0, `send` с `mid` (повтаря при мрежова
  грешка, без втори ход), `state` (план + задачи) в `BackendState`.
- Прозорец: бележка и име на хода от `progress` (под-агент); чипове „План“ и
  „2/5 · задача“ в горната лента (клик → `/plan` / `/todos`); командите на
  чата (/plan, /undo, /compact, /context, /todos, /review, /bg, /agents,
  /memory, /init, /commands, /hooks, /mcp) в менюто — `command()` в App.tsx
  ги връща като текст и агентът им отговаря в разговора (phone_command).
- Абонаментът Claude (`claude_cli`): `SUBSCRIPTION_PROVIDERS` в
  `desktop_commands.py` — не е „платени токени“ в /usage; значка АБОНАМЕНТ
  (`Badge` в Panels.tsx) вместо FREE/PAID.
- Проверено: ruff чист, mypy чист за двата модула, `npm run typecheck`,
  `npm test` 11/11, `npm run build`; pytest за remote_server + desktop_commands
  73 минават (2 падат и на main на Windows: `braked_address…`, `deeply_nested_json…`).
- CI: `gh workflow run native.yml --ref feat/desktop-oct12` → артефакти
  `genesis-windows-x64` и `genesis-desktop-setup` (вж. „Инсталирано“ по-долу).
- **Кой двигател тръгва** (поправено 12.10 вечерта, `src/main/exe.ts` + `tests/exe.test.ts`):
  прозорецът пускаше стария `Programs\Genesis\genesis.exe` (24.09), защото
  папката на инсталатора и `GENESIS_INSTALL_DIR` (слага я `install.ps1`, сочеше
  същата стара папка) бяха преди PATH. Сега редът е: настройката `exe` →
  `GENESIS_EXE` → `genesis` от PATH (каквото пуска терминалът: pipx
  `~\.local\bin`) → папката на pipx → инсталатора. Проверено с пускане:
  `exe = C:\Users\roika\.local\bin\genesis.exe`, `commands: false`.
- pipx Genesis (клон `claude/mega-upgrade-oct11`, f8f08d7) **няма** `--bind` и
  операцията `command` — те са само в този клон. С него чатът работи, а
  /usage, /model, /status казват „обнови Genesis“ и бутонът „⚡ днес“ липсва.
  Пълно ще стане, когато PR #60 влезе в main и pipx се преинсталира (НЕ докато
  върви bench — той ползва pipx Python-а).
- Снимки с двигателя от клона (CI артефакт, `GENESIS_EXE=…`), всичко изглежда
  добре: главен екран, /model (Claude абонамент „готов“), моделите на Claude
  със значка АБОНАМЕНТ, резервната верига, /status, /usage. Кликовете — през
  `npx electron . --remote-debugging-port=9333` и CDP скрипт (без промяна в кода).
- Прекият път „Genesis Agent“ (Start менюто) още отваря стария
  `Programs\Genesis\genesis.exe` — не е пипан.

## Направено
- Backend (`genesis_agent/remote_server.py`): опция `--bind`, за да слуша само на
  127.0.0.1 и Windows Firewall да не пита; разборът на аргументите е изведен
  в отделна функция, която има тестове (+5 теста в `tests/test_remote_server.py`)
- Electron main (`desktop/src/main/`): пуска и спира агента, криптиране,
  настройки, спира целия процес с дървото му (`taskkill /T`)
- Спиране на останал агент: pid-ът се пише в `%APPDATA%\Genesis\agent.json`,
  следващото пускане спира останалия `genesis serve`, но само ако командният
  ред съвпада точно с пуснатия от приложението
- UI (`desktop/src/renderer/`): странична лента, чат, поле за писане, карти
  за инструментите, markdown, екрани за старт, грешка и липсваща инсталация
- Икона (`scripts/make_icon.py`), dev режим (`npm run dev`), инсталатор NSIS

## Проверено (2026-09-25)
- `npm run typecheck`: ок
- `npm test`: 8/8 (криптирането съвпада с Python сървъра)
- `pytest tests/test_remote_server.py`: 35/35, ruff: ок
- Спирането на останал агент: ръчно направен сирак беше спрян при старт
- Инсталаторът `desktop/release/Genesis-Setup-0.1.0.exe` е инсталиран в
  `%LOCALAPPDATA%\Programs\genesis-desktop\Genesis.exe`
  (преки пътища „Genesis“ на десктопа и в Start менюто)

## Поправени бъгове
- 2026-09-25: при скриване на страничната лента (Ctrl+B) чатът изчезваше.
  `.main` нямаше колона в CSS решетката и попадаше в колоната на лентата, която при скриване е широка 0 px.
  Поправка: `.sidebar { grid-column: 1 }`, `.main { grid-column: 2 }` в
  `src/renderer/styles.css`. Проверено със снимки при отворена и скрита лента.

## ГОТОВО: всички `/` команди + прозорец „Разход“ (2026-09-25)
Потребителят поиска всички `/` команди от терминала в приложението и красив
прозорец за разхода (като usage/cost в Claude Code): колко е похарчено, колко остава.

### Готово и тествано
- **Сървър**, нов модул `genesis_agent/desktop_commands.py`: команди, които връщат
  данни (dict): status, usage, models, provider_models, set_model, maxcoding,
  local_max, local_normal, skills, tasks, done, drop, history, load_history,
  backup, update. `usage_report()` е чиста функция: периоди днес/7д/Nд/общо,
  по дни, по доставчик и модел, платени токени, квоти (KNOWN_QUOTAS:
  ollama_cloud ~5M/седмица, cerebras 1M/ден, взети от коментарите в кода).
  Дните се броят по местно време.
- `remote_server.py`: op `"command"` (name, arg); `hello()` връща
  `features: ["commands"]`; `clear()` вече започва нов файл в историята и нулира броячите.
- Тестове: `tests/test_desktop_commands.py` (7) + 2 в `test_remote_server.py`.
  Общо 44 минават, ruff е чист.
- **Приложение**: `CommandResult` и типовете на отчетите в `shared/types.ts`;
  `api.command()` през preload, main и `backend.command()`. Проверява
  `state.commands` и връща `old_agent`, ако exe-то е старо.
  - `Composer.tsx`: 20 команди с групи и синоними (`/cost`, `/разход`…),
    аргументи (`/done <номер>`), менюто скролва.
  - `components/Panels.tsx`: `Sheet` с изгледи usage (пари $0, плочки,
    квоти, контекст, графика по дни с подсказка, ленти по доставчик, топ модели),
    status (с превключватели на режимите), models (избор на доставчик и модел с търсене, верига),
    history (зареждане), update, help, text (skills/tasks).
  - `App.tsx`: `command()` насочва всяка команда; бутон в горната лента
    „⚡ 82K днес · $0“ отваря /usage; бутонът с модела отваря /model; Esc затваря прозореца.
  - `lib/format.ts` (fmtTokens, niceMax) + тест. `npm test`: 9/9, typecheck: ок, `npm run build`: ок.
  - Стилове: краят на `styles.css` („`/` command sheets“).

### Инсталирано (2026-09-25, 01:16) ✅
- Smart App Control е ВКЛЮЧЕН и блокира всичко неподписано, сглобено ЛОКАЛНО
  (`genesis.exe` от PyInstaller и `Genesis-Setup` от electron-builder; CodeIntegrity 3033/3077).
  Сглобеното в GitHub CI минава. Не изключвай SAC: после не може да се включи отново.
- **Правило: сглобявай в CI, не локално.** Клон `feat/desktop-app` (качен в origin):
  `gh workflow run native.yml -R me7ko-dev/genesis-agent --ref feat/desktop-app`
  → артефакти `genesis-windows-x64` (zip + sha256) и `genesis-desktop-setup` (инсталатор).
  Release няма: publish върви само при push в main. Задачата `desktop` е в native.yml,
  защото ръчно пускане (dispatch) работи само за workflow, който вече е в main.
- Инсталиране: `gh run download <id> -n ...`; genesis.exe → разархивиране в
  `%LOCALAPPDATA%\Programs\Genesis` (резервно копие на стария:
  `Programs\Genesis.bak-native-build-9`); инсталаторът с `/S` СЛЕД като приложението е затворено.
  Проверка: `app.asar` съдържа `sheet-back`.
- Проверено със снимки: /usage (горе, графика, доставчици, модели), /status,
  /model, /models, /help. Реални данни: 2,93M токена, $0, остават 4,78M от
  седмичната квота на Ollama Cloud.
- Снимки: `GENESIS_DESKTOP_SHEET=usage` + `GENESIS_DESKTOP_SHOT=out.png,24000[,scrollPx]`
  (агентът тръгва за ~15–20 s).

### Остава / идеи
- PR от `feat/desktop-app` към main (само ако потребителят каже) → тогава release
  ще съдържа и новия genesis.exe; инсталаторът на приложението да влезе в release
- Не е пробвано на живо: избор на модел с клик, /history зареждане, превключвателите
  на режимите (сървърната част има тестове)
- Подписване на инсталатора

## Зависимости
- Нужен е CLI-ят Genesis: приложението пуска същия `genesis`, който пуска
  терминалът (PATH; на лаптопа — pipx в `~\.local\bin`), а
  `%LOCALAPPDATA%\Programs\Genesis\genesis.exe` (24.09, от `feat/desktop-app`)
  е само резерва. Прекият път „Genesis Agent“ още отваря него.
- С genesis без `features: ["commands"]` (и сегашния pipx) приложението работи,
  а `/` командите казват „обнови Genesis“.

## Следващи стъпки
1. Потребителят да пробва приложението и да каже какво да се промени
2. PR към main (само ако потребителят поиска)
3. Инсталаторът да влезе в release до `genesis-windows-x64.zip`
4. Автоматично обновяване, подписване на инсталатора

## Полезни команди (от `desktop/`)
```
npm run dev        # прозорец с hot reload
npm run build      # main + renderer
npm run dist       # release/Genesis-Setup-0.1.0.exe
npm test; npm run typecheck
```
Отладка: `GENESIS_DESKTOP_DEBUG=1`, снимка на екрана: `GENESIS_DESKTOP_SHOT=<път.png>`.
