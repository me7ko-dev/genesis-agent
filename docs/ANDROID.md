# Genesis на телефона — без компютър (Android)

Агентът работи на самия телефон, точно както на компютъра: пише и пуска
код, ползва python, git и node, помни между разговорите. Компютърът може
да е изключен.

```
 приложението Genesis Remote  ── 127.0.0.1, криптирано ──►  Termux: genesis phone
   чат, потвърждения, Стоп                                      същият агент като `genesis`
```

Android сам по себе си няма python, git и node. **Termux** ги дава: истински
Linux за телефона, безплатен, с отворен код. Genesis тече в него, а
приложението Genesis Remote е прозорецът към него. Устройството е като
при Genesis Desktop на Windows.

> iPhone не може да пуска програми вътре в приложение. Там Genesis работи
> само с компютър ([MOBILE.md](MOBILE.md)).

---

## Инсталиране (веднъж, 5–10 минути)

1. **Termux от F-Droid:** <https://f-droid.org/packages/com.termux/>.
   Версията от Google Play също става; старата (от 2020) не става.
2. **Приложението Genesis Remote.** Свали `genesis-remote-android.apk` от
   [последния release](https://github.com/me7ko-dev/genesis-agent/releases/latest)
   и го инсталирай (разреши „инсталиране от този източник“).
3. Отвори Genesis Remote → **„Без компютър — на този телефон“** →
   **Копирай**. Отвори Termux, постави реда и натисни Enter:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/me7ko-dev/genesis-agent/main/scripts/install-termux.sh | bash
   ```

4. Скриптът пита за достъп до паметта на телефона и за ключовете на
   моделите: Enter = ще ги пренесеш от компютъра (виж по-долу), „р“ =
   пишеш ги на ръка (`genesis setup`).
5. Накрая Termux сам отваря Genesis Remote, вече свързан. Готово.

### Ключовете от компютъра (QR код)

На компютъра:

```powershell
genesis keys qr
```

Проверява всеки ключ, с който Genesis работи там (`~/.genesis/.env`), и
отваря в браузъра QR код **само с работещите**. На телефона: Genesis Remote →
меню ⋯ → **Ключове от компютъра** → насочи камерата → **Запиши**. Ключовете
отиват до Genesis на телефона по криптираната връзка. Приложението не ги
помни, а страницата с кода на компютъра се трие.

- `genesis keys test` — само проверка, без QR.
- `--from файл.txt` — ключове от текстов файл вместо от Genesis: редове
  `ИМЕ=стойност` или просто ключове един под друг (познават се по
  началото: `gsk_` Groq, `sk-or-` OpenRouter, `nvapi-` NVIDIA, `csk-`
  Cerebras, `AIza` Gemini).
- Пренасят се само ключове за модели (и за известията в Telegram). Други
  токени от компютъра не.

Първия път Android може да поиска още едно разрешение: **„Изпълнение на
команди в Termux“**. Разреши го — с него приложението пуска Genesis само.
Ако не пита: Настройки → Приложения → Genesis Remote → Разрешения →
Допълнителни разрешения.

### Автоматично при включване на телефона (по желание)

Инсталирай **Termux:Boot** от същото място като Termux
(<https://f-droid.org/packages/com.termux.boot/>) и го отвори веднъж.
Скриптът вече е сложил `~/.termux/boot/genesis`.

## Всеки ден

Отваряш Genesis Remote и пишеш. Ако агентът не тече, приложението го
пуска само (или с бутона **„Пусни Genesis“**). Спиране: меню ⋯ →
„Спри Genesis на телефона“.

Докато Genesis тече, Termux показва известие. Така Android не го
приспива при изгасен екран.

В Termux:

| Команда | Какво прави |
|---|---|
| `genesis phone start` | пуска агента във фонов режим |
| `genesis phone stop` | спира го |
| `genesis phone status` | тече ли, къде работи |
| `genesis phone pair` | отваря приложението, свързано с агента |
| `genesis phone log` | последното от изхода му (при проблем) |
| `genesis setup` | ключовете за моделите |
| `genesis` | обикновеният чат в терминала |

**Обновяване:** постави реда за инсталиране още веднъж.

## Къде са файловете

- Работната папка на агента е `~/genesis` в Termux. Там git, npm и
  `./скрипт` работят. В паметта на телефона не биха работили: Android не
  позволява там изпълними файлове.
- Готовите файлове агентът може да сложи в **Изтегляния**
  (`~/storage/downloads`) или другаде в паметта на телефона
  (`~/storage/shared`). Там ги виждат и другите приложения. Агентът знае
  тези пътища.
- Ключове и памет: `~/.genesis`, както на компютъра.

## Разлики с компютъра

- **Моделите са само облачните** (Groq, Ollama Cloud, Cerebras, Gemini…).
  Локалните модели на Ollama искат видеокарта.
- Програмите се слагат с `pkg install …` (не `apt`/`sudo`). Агентът знае,
  че е в Termux, и пита, преди да инсталира.
- Тежки задачи (голям `npm install`, компилиране) вървят по-бавно и харчат
  батерия.

## Ако нещо не тръгва

| Какво виждаш | Какво да направиш |
|---|---|
| „Genesis на телефона е спрян“ и „Пусни“ не помага | В Termux: `genesis phone log` показва защо. Най-често липсват ключове → `genesis setup`. |
| „Termux не прие командата“ | Постави реда за инсталиране още веднъж. Той разрешава на приложението да пуска Genesis (`allow-external-apps`). |
| Termux: „[Process completed (signal 9)]“ | Android 12+ спира фоновите процеси на Termux. Android 14+: Настройки → За разработчици → „Disable child process restrictions“. Android 12–13: [инструкции](https://github.com/agnostic-apollo/Android-Docs/blob/master/en/docs/apps/processes/phantom-cached-and-empty-processes.md). |
| Агентът заспива при изгасен екран | Настройки → Приложения → Termux → Батерия → „Без ограничения“. |
| „CANNOT LINK EXECUTABLE“ в Termux | `pkg upgrade`, после редът за инсталиране пак. |

## Как е направено (за разработка)

- `genesis_agent/phone.py` — `genesis phone`: пуска `genesis serve --bind
  127.0.0.1` във фонов режим, pid, изход в `~/.genesis/phone/serve.log`,
  wake lock, връзката `genesisremote://pair?u=…` за приложението.
- `scripts/install-termux.sh` — пакетите, venv в `~/.genesis/venv`
  (`--system-site-packages` заради `python-cryptography` от Termux),
  `allow-external-apps`, Termux:Boot.
- `mobile/modules/termux-bridge` — малък модул за Android: приложението
  пуска `genesis phone start` през
  [RUN_COMMAND](https://github.com/termux/termux-app/wiki/RUN_COMMAND-Intent).
- `mobile/src/lib/phone.ts` — режимът „на този телефон“ в приложението.
- Разлики от Linux в самия агент: обвивката е `$PREFIX/bin/sh` (няма
  `/bin/sh`), sandbox-ът пропуска `LD_PRELOAD`/`TERMUX_*` (иначе
  `#!/usr/bin/env node` не тръгва), пътищата за папките на телефона.
- CI: задачата `termux` в `native.yml` пуска инсталацията, `genesis phone`,
  протокола на приложението и командите на агента в истински Termux
  (`termux/termux-docker`) — `scripts/termux_smoke.sh`.
