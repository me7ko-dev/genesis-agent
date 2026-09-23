# Genesis Agent на Windows

Проверено на 2026-09-20: пакетът се сглобява цял (20 умения, `config.yaml`,
`gui/`), инсталира се от този клон с командата по-долу и `genesis skills`
отговаря от инсталираното копие. CI пуска целия пакет тестове и на
`windows-latest`, не само на Linux.

Каквото НЕ е проверено на жив Windows, е казано направо в „Какво е различно
на Windows" накрая. Тази машина е Linux; всичко Windows-специфично тук идва от
кода (`genesis_agent/sandbox.py`, `paths.py`) и от CI, не от изпълнение.

---

## Какво ти трябва преди това

| | Защо |
|---|---|
| **Python 3.10+** | от python.org, с отметка „Add python.exe to PATH". Ако при `python` ти се отваря Microsoft Store — това е заглушката, не Python. |
| **Git for Windows** | не за инсталацията, а за работата: sandbox-ът пуска командите през Git Bash. Без него пада към `cmd.exe` и половината shell команди се държат другояче. |
| **Windows Terminal** | конзолата по подразбиране е cp866/cp1251 и не показва кирилица. Агентът пише на български. |

---

## Инсталация

### Вариант 1 — скриптът (проверява и трите неща отгоре)

```powershell
curl.exe -L -o install_windows.ps1 https://raw.githubusercontent.com/me7ko-dev/genesis-agent/claude/token-upgrade-ipe4yg/scripts/install_windows.ps1
powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
```

Скриптът намира Python (пита го за версията, не се доверява на PATH), казва ти
дали има Git Bash, инсталира през pipx и накрая извиква `genesis --version` по
пълен път — защото PATH в текущия прозорец още не знае за новата команда.

### Вариант 2 — на ръка, три команди

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

**GUI-то не тръгва.** `genesis gui` и `genesis jarvis` искат GTK4/libadwaita,
което го има само на Linux. CLI-ят и всичко останало работят.

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
