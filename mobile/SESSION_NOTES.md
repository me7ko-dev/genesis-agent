# Genesis на Android без компютър: докъде сме стигнали

Последно обновено: 2026-09-28

## Какво е това
Потребителят иска Genesis да работи на телефона сам, без включен компютър,
както Windows приложението (Genesis Desktop). Избра **Termux + APK**:
агентът тече в Termux (Linux за Android: python, git, node), а
приложението Genesis Remote (`mobile/`) е прозорецът към него. Устройството е
като при Desktop: `genesis serve --bind 127.0.0.1` + криптираният протокол.

Ръководство за потребителя: `docs/ANDROID.md`.

## Къде се намира
- Worktree: `C:\Users\roika\Projects\genesis-android`, клон
  `feat/android-standalone` (от `origin/feat/desktop-app` + слят `origin/main`)
- Репо: me7ko-dev/genesis-agent. **Няма PR, нищо не е в main.**
- Локално няма Java, Android SDK и Docker → APK и Termux проверката са само в CI:
  `gh workflow run native.yml -R me7ko-dev/genesis-agent --ref feat/android-standalone`
  (задачи `android` → артефакт `genesis-remote-android`, `termux` → проверката).
  `mobile.yml` също се пуска ръчно (типове, тестове срещу Python сървъра, iOS).

## Направено
- `genesis_agent/phone.py` — `genesis phone start|stop|status|pair|log`
  (фонов serve, pid/лог в `~/.genesis/phone/`, wake lock, SIGINT при спиране,
  `pair` отваря `genesisremote://pair?u=<pairing url>`)
- `scripts/install-termux.sh` — един ред в Termux: пакети, venv
  `~/.genesis/venv` (`--system-site-packages` заради python-cryptography),
  `allow-external-apps = true`, `~/.termux/boot/genesis`, `genesis setup`, pair
- Агентът на Android: `$PREFIX/bin/sh` при липса на `/bin/sh`; sandbox-ът
  пропуска `LD_PRELOAD`/`TERMUX_*` и сам зарежда termux-exec, ако го няма;
  `pkg install` = CONFIRM; `env_facts` показва `~/storage/...`; името е
  моделът на телефона; `genesis serve --link`
- Приложението: раздел „Без компютър — на този телефон“ (Копирай реда,
  F-Droid, Отвори Termux, Свържи), сдвояване по deep link, само пуска агента
  при „няма връзка“ (+ бутон „Пусни Genesis“), меню „Спри Genesis на телефона“.
  Модул `mobile/modules/termux-bridge` (Kotlin, RUN_COMMAND, `<queries>`).
  Командата за инсталиране съдържа клона, от който е сглобено APK-то
  (`EXPO_PUBLIC_GENESIS_REF` в native.yml).
- Тестове: `tests/test_phone.py` (+ sandbox/remote_server/agent_core).
  CI `termux`: `scripts/termux_smoke.sh` в `termux/termux-docker:x86_64`.

## Ключовете от компютъра (2026-09-28, по молба на потребителя)
- `genesis_agent/keys_transfer.py`: `genesis keys test|qr|import`. `qr` пробва
  всеки ключ и слага в QR само работещите (`genesisremote://keys?gq=…&ol3=…`,
  кратки имена → ~97 модула), страницата се трие след Enter. Пренасят се
  само ключове на доставчици на модели (+ Telegram); `CLAUDE_CODE_MESSAGING_TOKEN`
  и `GENESIS_WAITLIST_TOKEN` от средата на лаптопа НЕ.
- Сървър: op `import_keys` → `keys_transfer.save()` (сливане в .env,
  резервно копие, 0600, веднага в os.environ и gta.KEYS); `status.keys` = брой.
- Приложение: екран `src/app/keys.tsx` (камера / deep link), меню „Ключове от
  компютъра“, банер при `keys === 0`, скенерът за сдвояване разпознава и този код.
- Инсталаторът: Enter = ключовете от компютъра по-късно, „р“ = `genesis setup`.
- Лаптопът: ключовете на Genesis са в `~/.genesis/.env` (15), а на десктопа има
  `апита.txt` (10, всичките вече в .env). `genesis keys test` на 2026-09-28:
  12 работят (5 Ollama Cloud, 3 Groq, 3 OpenRouter, 1 NVIDIA); 3-те Cerebras
  връщат 402 (акаунтът иска плащане) и не влизат в QR кода.
- Пряк път на десктопа „Genesis ключове за телефона“ → `.venv` в worktree-то
  (`qrcode` го няма в системния Python и в pipx) → `python -m genesis_agent.cli keys qr`.
  Инсталираният genesis.exe още няма `keys` (ще го има след release от main).

## Проверено (2026-09-28)
- Локално (Windows): ruff чист; целият pytest: 1719 passed, 19 skipped;
  `tsc` ок; чат тестовете 6/6. `interop.test.ts` виси на Windows
  (`new URL().pathname` → `/C:/...`) — стар проблем, в CI минава.
- CI native.yml, run 36396663010 (commit „fix(android): load termux-exec…“):
  termux ✅, android ✅, windows ✅, desktop ✅. mobile.yml run 36396329402:
  test ✅ (вкл. interop с Python сървъра), ios ✅.
- В истински Termux (termux-docker): install-termux.sh, `genesis phone
  start/status/stop`, протоколът на приложението (status + command),
  sh/python/node/git, `#!/usr/bin/env node`, pytest test_phone +
  test_remote_server (52) — всичко минава. В docker няма LD_PRELOAD →
  sandbox-ът зарежда `libtermux-exec-ld-preload.so` сам.
- В Termux `sys.platform == "android"` (Python 3.13+); в кода няма проверки за „linux“.
- APK-то (артефакт `genesis-remote-android` от run 36396663010, sha256
  c0ebab9d…ba87) съдържа RUN_COMMAND разрешението, `<queries>` за com.termux,
  схемата genesisremote и командата за клона feat/android-standalone.

- Ключовете: CI native.yml run 36402553765 (termux ✅ вкл. import_keys в истински Termux, android ✅, windows ✅, desktop ✅), mobile.yml run 36402557778 (test ✅, ios ✅). APK-то е в `Downloads\genesis-remote-android-bez-kompyutar.apk`.

## На живо на телефона на потребителя (2026-09-28)
- Телефон: Samsung Galaxy S10+ (SM-G975F), Android 12, Termux 0.118.3, aarch64.
- Достъп от лаптопа: SSH в Termux, `ssh -p 8022 -i ~/.ssh/genesis_phone_ed25519 u0@192.168.0.86`
  (Wi-Fi `Me7ko_5G`; лаптопът е и на `Tenda_4CED25_5G`, която телефонът не вижда).
  sshd се пуска в Termux с `sshd`; ключът е само в ~/.ssh/authorized_keys там.
- Беше Genesis 0.1.0 (pip, юли) → оставен като `~/.local/bin/genesis.old-0.1.0`;
  новият е в `~/.genesis/venv`, `$PREFIX/bin/genesis`. ~/.genesis (памет, умения) запазени.
- Ключове: старият OLLAMA_API_KEY на телефона → OLLAMA_API_KEY_2 (различен от
  тези на лаптопа, работи) + 12-те от лаптопа по SSH. `genesis keys test` на
  телефона: 13/13 ✅. Копие преди това: `~/.genesis/.env.before-laptop-keys`.
- **Бъг, хванат само на истинския телефон:** RLIMIT_AS 2 GB убива всеки процес
  на Android arm64 (rc=-6) → на Android без RLIMIT_AS (d580b49).
- **Бъг:** повторното пускане на инсталатора не обновяваше (pip пропуска
  същата версия) → `--force-reinstall --no-deps` (bae892e).
- Истински ходове през протокола: WRITE_FILE/LIST_DIR (3 s), писане + пускане на
  python (5 s, верен резултат). Моделът: groq/openai/gpt-oss-120b.
- APK: копиран в Termux (`~/genesis-remote.apk`), инсталаторът отворен с `termux-open`.
- APK инсталиран (13:4x). Сдвояването през `genesis phone pair` (deep link от
  Termux) НЕ стигна до приложението — най-вероятно Android блокира отварянето
  на activity от Termux, когато Termux не е на преден план (по SSH, или ако
  потребителят се е върнал в приложението, докато инсталаторът върви).
  Сдвоих го с QR на екрана на лаптопа (`genesis serve --bind 127.0.0.1 --link`)
  → скенерът в приложението. Работи.
- ✅ Автоматичното пускане (RUN_COMMAND) работи наживо: `genesis phone stop` в
  13:53:12 → приложението пусна Genesis отново в 13:53:20.
- Идея за по-сигурно сдвояване: RUN_COMMAND с PendingIntent за резултата
  (`genesis serve --link` → stdout право в приложението), без смяна на приложения.
  Или разрешение „Показване върху други приложения“ за Termux.
- Приложението е преименувано на „Genesis“ (пакетът остава dev.me7ko.genesisremote).
- Клавиатурата закриваше полето за писане: приложението е edge-to-edge
  (задължително в Expo 57) → `KeyboardAvoidingView behavior="padding"` и на
  Android (1ff… „fix(app): the keyboard…“). Новото APK (run 36412857012) е
  инсталирано на телефона — да се потвърди от потребителя, че вече вижда полето.
- „Пълен достъп“ (по молба на потребителя): Termux:API (беше инсталиран) +
  `pkg install termux-api`; разрешенията от потребителя: показване върху други
  приложения, файлове (връзките в ~/storage направени на ръка — termux-setup-storage
  по SSH не показва прозорец), батерия без ограничения, всичко за Termux:API
  (проверено: контакти, SMS, обаждания, телефон, местоположение, камера, Wi-Fi ✅).
  Termux:Boot 0.8.1 от F-Droid инсталиран и отворен веднъж.
- Агентът знае за termux-* командите (env_facts); SMS/обаждане/споделяне и
  личните данни са CONFIRM в sandbox-а (00b63ab). Наживо: батерия + известие ✅.
- Разрешенията се дават най-лесно с известия (`termux-notification --action "am start …"`)
  или `am start … APPLICATION_DETAILS_SETTINGS -d package:…` по SSH.
- sshd в Termux остава пуснат (само с ключ); при рестарт на телефона не тръгва сам.

## Остава
- Потребителят да пробва на истински телефон (APK от CI артефакта)
- Не е пробвано на живо: RUN_COMMAND бутонът, deep link сдвояването,
  Termux:Boot, работа при изгасен екран
- Пренасянето на ключове не е пробвано на живо (камера → Запиши)
- PR към main само ако потребителят каже → тогава release съдържа APK-то с тази версия
