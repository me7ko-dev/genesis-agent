# Genesis Desktop: докъде сме стигнали

Последно обновено: 2026-09-25

## Какво е това
Настолно приложение за Windows (Electron + React + Vite) около Genesis агента,
в стила на Claude Code. Приложението пуска `genesis.exe serve` на 127.0.0.1 и
говори с него по криптирания протокол на `remote_server.py` (същия като на мобилното приложение).

## Къде се намира
- Worktree: `C:\Users\roika\Projects\genesis-desktop`, клон `feat/desktop-app`
  (rebase върху `origin/main` c80cfa4, качен в origin)
- Основното репо: `C:\Users\roika\Projects\genesis-agent` (GitHub: me7ko-dev/genesis-agent)
- Кодът на приложението: `desktop/`
- Commit-ите са качени в клона. **Няма PR и нищо не е влязло в main.**

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
- Нужен е CLI-ят Genesis в `%LOCALAPPDATA%\Programs\Genesis\genesis.exe`.
  Сега там е сглобеният в CI от `feat/desktop-app` (има `--bind` и командите).
  Прекият път „Genesis Agent“ отваря него (терминала).
- С по-стар genesis.exe (без `features: ["commands"]`) приложението работи, а
  `/` командите казват „обнови Genesis“.

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
