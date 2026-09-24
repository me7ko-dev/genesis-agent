# Genesis Agent на Windows

Проверено на 2026-09-20: пакетът се сглобява цял (20 умения, `config.yaml`,
`gui/`), инсталира се от този клон с командата по-долу и `genesis skills`
отговаря от инсталираното копие. CI пуска целия пакет тестове и на
`windows-latest`, не само на Linux.

Каквото НЕ е проверено на жив Windows, е казано направо в „Какво е различно
на Windows" накрая. Тази машина е Linux; всичко Windows-специфично тук идва от
кода (`genesis_agent/sandbox.py`, `paths.py`) и от CI, не от изпълнение.

---

## Родно приложение — един ред, без Python (препоръчително)

Като Claude Code: в PowerShell

```powershell
irm https://raw.githubusercontent.com/me7ko-dev/genesis-agent/main/scripts/install.ps1 | iex
```

и после, в папката, в която искаш да работиш:

```powershell
genesis setup      # ключовете, веднъж
genesis
```

Какво прави редът (`scripts/install.ps1`):

- сваля `genesis-windows-x64.zip` от последния GitHub release и го сверява
  със SHA256 файла до него;
- разархивира до `%LOCALAPPDATA%\Programs\Genesis.new`, пуска новия
  `genesis.exe --version` и **чак тогава** го слага на мястото на стария —
  счупено сваляне или антивирус, който е махнал файл, не пипат работеща
  инсталация;
- слага папката в потребителския PATH (работи веднага в същия прозорец),
  добавя „Genesis Agent" в Start менюто (в Windows Terminal, ако го има) и в
  Settings → Apps, откъдето се маха като всяко приложение;
- проверява Git Bash (предлага `winget install Git.Git`, ако липсва) и казва
  има ли системен Python за тестовете на твоите проекти.

Не иска администратор, Python или pipx. `genesis.exe` носи собствен Python
заедно с цялата стандартна библиотека, requests, PyYAML, rich, google-auth,
anthropic и cryptography — sandbox-ът изпълнява код през него, така че
агентът работи и на машина без Python. Тестовете на ТВОЯ проект (`genesis
fix`, pytest) пак минават през истинския Python на машината, защото само той
има пакетите на проекта — `paths.project_python()` го намира през `py -3`.

**Работна папка.** Като `claude`: там, откъдето е пуснат. Ако е пуснат от
домашната папка, от корена на диска, от Windows или от Start менюто —
`~\.genesis\workspace`, за да не получи агентът целия профил. `GENESIS_WORKSPACE`
го задава изрично.

**От телефона.** `genesis serve` показва QR код за приложението Genesis
Remote (Android, iPhone) — виж [MOBILE.md](MOBILE.md). Уеб версията за iPhone
е вградена в `genesis.exe`.

**Обновяване.** `/update` в чата: проверява последния release, показва какво
носи и след `exit` сваля и подменя приложението на заден план (същият
инсталатор, копиран в `%TEMP%`, защото папката на приложението се заменя
цяла). Следващото `genesis` казва дали е минало. Ръчно — същият ред отгоре.

**Махане.** Settings → Apps → Genesis Agent, или

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Genesis\_internal\install.ps1" -Uninstall
```

Ключовете, паметта и уменията в `~\.genesis` остават; `-Purge` маха и тях.

**Откъде идва.** `.github/workflows/native.yml` сглобява `genesis.exe` с
PyInstaller (`packaging/genesis.spec`) на всеки PR и го публикува като release
при всеки push в `main`. Преди публикуване CI пуска самия `genesis.exe`
(`packaging/build.py`: CLI, Python режим, sandbox, стандартна библиотека),
после инсталира от zip-а, обновява върху инсталацията, докато старият
`genesis.exe` още тече, и деинсталира. Локален билд:

```powershell
py -m pip install ".[google,premium,signing]" pyinstaller
py packaging\build.py
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -ZipPath dist\genesis-windows-x64.zip
```

**SmartScreen / антивирус.** `genesis.exe` не е подписан. Свален през
`irm`/`Invoke-WebRequest`, той няма „Mark of the Web" и SmartScreen не пита.
Билдът е папка, не един .exe, и без UPX — двете най-чести причини за фалшива
тревога при PyInstaller. Ако антивирусът все пак махне файл, инсталаторът го
хваща при пробното стартиране и не подменя старата версия.

---

## Чрез pipx (ако искаш Genesis в собствения си Python)

### Какво ти трябва преди това

| | Защо |
|---|---|
| **Python 3.10+** | от python.org, с отметка „Add python.exe to PATH". Ако при `python` ти се отваря Microsoft Store — това е заглушката, не Python. |
| **Git for Windows** | не за инсталацията, а за работата: sandbox-ът пуска командите през Git Bash. Без него пада към `cmd.exe` и половината shell команди се държат другояче. |
| **Windows Terminal** | конзолата по подразбиране е cp866/cp1251 и не показва кирилица. Агентът пише на български. |

---

### Инсталация

#### Вариант 1 — скриптът (проверява и трите неща отгоре)

```powershell
curl.exe -L -o install_windows.ps1 https://raw.githubusercontent.com/me7ko-dev/genesis-agent/claude/token-upgrade-ipe4yg/scripts/install_windows.ps1
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
```

Скриптът намира Python (пита го за версията, не се доверява на PATH), казва ти
дали има Git Bash, инсталира през pipx и накрая извиква `genesis --version` по
пълен път — защото PATH в текущия прозорец още не знае за новата команда.

#### Вариант 2 — на ръка, три команди

```powershell
py -m pip install --user pipx
py -m pipx install --force "git+https://github.com/me7ko-dev/genesis-agent@claude/token-upgrade-ipe4yg"
py -m pipx ensurepath
```

После **отвори нов терминал** (старият има стар PATH) и:

```powershell
genesis setup
genesis
```

`genesis setup` пита за всеки ключ, прави по една истинска заявка да го провери
и записва `C:\Users\<ти>\.genesis\.env`.

---

## Първи тест, че работи

```powershell
genesis --version          # genesis-agent 0.1.0
genesis skills             # 20 умения, 16 verified
genesis models             # веригата модели; --refresh я пресканира
genesis mission "напиши функция, която обръща думите в изречение"
```

`genesis mission` е най-честният тест: минава през мозък, sandbox, верификатор
и записване на умение — тоест през всичко наведнъж.

---

## Google (Gemini и Vertex) на Windows

Gemini през AI Studio иска само ключ:

```powershell
setx GEMINI_API_KEY "твоят-ключ"
```

(или го сложи през `genesis setup`, за да иде в `.env` с останалите).

Vertex AI няма статичен ключ — достъпът е OAuth токен от Google Cloud:

```powershell
py -m pipx inject genesis-agent google-auth
gcloud auth application-default login
setx GOOGLE_CLOUD_PROJECT "твоят-проект"
```

`pipx inject` е нужен, защото pipx държи Genesis в собствена среда — обикновен
`pip install google-auth` отива другаде и Vertex остава „не е настроен".
Няколко проекта: `GOOGLE_CLOUD_PROJECT_2`, `_3` … Всеки има собствена квота и
собствена сметка; ротацията между тях е в `genesis_agent/brain.py`.

---

## Обновяване и махане

Най-лесно — вътре в чата:

```
❯ /update
```

Проверява GitHub, показва стар→нов комит, пита за потвърждение и при „да"
насрочва обновяването на заден план. Не тръгва веднага нарочно: `pipx
install --force` подменя точно `genesis.exe`, който в момента тече, а на
Windows заключен `.exe` не може да бъде презаписан. Обновяването довършва
чак СЛЕД като излезеш (`exit`) — следващото `genesis` казва дали е минало.
Проверено само по логика (mock-нати тестове, `genesis_agent/self_update.py`);
истинско заключване на файл, докато тече, не може да се симулира на Linux —
ако `/update` се държи странно на жива Windows машина, това е първото място
за поглед, а долният ръчен път винаги работи като резерва.

Ръчно, същото нещо, без чат:

```powershell
py -m pipx install --force "git+https://github.com/me7ko-dev/genesis-agent@claude/token-upgrade-ipe4yg"
py -m pipx uninstall genesis-agent
```

`--force` преинсталира от нулата — иначе pipx вижда същата версия `0.1.0` и не
пипа нищо, въпреки че кодът в клона се е сменил.

---

## Какво е различно на Windows

**Лимитите на sandbox-а са по-слаби.** Ограниченията за CPU, памет и размер на
файл идват от POSIX `setrlimit` и на Windows просто не се прилагат
(`genesis_agent/sandbox.py`, `_preexec`). Това, което работи еднакво и на двете
платформи, е класификацията на командите (SAFE / CONFIRM / BLOCKED) и timeout-ът
с убиване на процеса. Тоест: опасната команда пак ще бъде спряна преди да се
пусне; безкрайният цикъл пак ще бъде убит по timeout; но скрипт, който изяде 8 GB
преди timeout-а, на Windows няма кой да го спре по-рано.

**`bash.exe` от WindowsApps не е обвивка.** Това е стартерът на WSL — друга
операционна система, друга файлова система, друг Python. Кодът нарочно го
отказва и търси Git Bash (`sandbox._is_wsl_launcher`); скриптът по-горе прави
същата проверка. Ако видиш тестове, които падат с „The RPC call contains a
handle that differs from the declared handle type", това е WSL, не Genesis.

**Конзолата.** Всеки entrypoint вика `paths.ensure_utf8_streams()`, което оправя
Python-ската страна. Другата страна е самата конзола — `chcp 65001` или
Windows Terminal.
