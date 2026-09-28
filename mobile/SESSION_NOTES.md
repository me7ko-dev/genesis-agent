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

## Остава
- Потребителят да пробва на истински телефон (APK от CI артефакта)
- Не е пробвано на живо: RUN_COMMAND бутонът, deep link сдвояването,
  Termux:Boot, работа при изгасен екран
- Пренасянето на ключове не е пробвано на живо (камера → Запиши)
- PR към main само ако потребителят каже → тогава release съдържа APK-то с тази версия
