# Задачи за лаптопа и за оператора (обновено 2026-11-15)

`HANDOFF.md` е от септември (PR #1) и вече не описва нищо текущо. Тук е какво
остава след PR #120, #122 и #123 (слети на 15.11, main = `b118dbd`) — само неща,
които CI и облачна сесия не могат: Windows, истински ключове, Cloudflare,
сървър, Stripe live, телефон, микрофон. Планът и измереното остават в
`NEXT_STEPS.md`; тук са стъпките.

Отбелязвай свършеното с ~~зачеркване~~ и дата, не добавяй нов текст.

## А. Агентът на лаптопа ги пуска сам

### ~~А.1 Genesis от pipx = последният main~~ (15.11: `b118dbd`)

```powershell
$py = "$HOME\AppData\Local\pipx\pipx\venvs\genesis-agent\Scripts\python.exe"
& $py -m pip install --force-reinstall --no-deps "git+https://github.com/me7ko-dev/genesis-agent@main"
Set-Location $HOME; & $py -m genesis_agent.cli --version   # → (<комит>, main)
```
- `--no-deps` и `pip` в самия venv, НЕ `pipx install --force`: той трие
  добавките (voice, qrcode, pillow, playwright, ruff, cryptography, anthropic…),
  които са сложени с `pipx inject`. Ако `pyproject.toml` е сменил зависимостите
  (`git diff <стар комит> main -- pyproject.toml`), пусни без `--no-deps`.
- Версията се пуска извън checkout-а (`Set-Location $HOME`): в папката на репото
  `python -m genesis_agent` взима кода от checkout-а, не от pipx.
- **Smart App Control спира `genesis.exe`** („An Application Control policy has
  blocked this file“, 15.11 — и преди обновяването). Докато е така:
  `& $py -m genesis_agent.cli` е същото без exe.

### ~~А.2 Новите тестове на Windows~~ (15.11: 92 passed, 2 skipped)

```powershell
python -m pytest -q tests/test_cloud_retention.py tests/test_cloud_upload.py tests/test_audit_2026_11_15_claims.py
```
В pipx venv-а няма pytest — пуска се със системния Python от checkout-а.

### А.3 Сайтът (`site/`) — качване в Cloudflare Pages

Промяна от последното качване (10.11): `site/public/index.html` — четирите
примера с файл на входа (#120). Сайтът не се качва сам при сливане.

```bash
cd site && npx wrangler pages deploy public --project-name genesis-waitlist --branch main
```
- Проектът е в Cloudflare акаунта, в който е KV-то от `site/wrangler.toml` — НЕ
  в акаунта на другите сайтове. В другия има празен проект със същото име
  (адрес `-6j8`): качване там не е грешка, а тихо отива на грешното място.
- Входът на wrangler за другите сайтове да не се сменя: отделна папка за входа,
  `XDG_CONFIG_HOME=<празна папка> npx wrangler login`, после `whoami` и
  `pages project list` — домейнът трябва да е `genesis-waitlist.pages.dev` без
  наставка. Накрая `wrangler logout` със същата папка.
- **15.11 не мина:** `wrangler login` одобрява с акаунта, който е влязъл в
  браузъра, а там беше другият → `whoami` показа грешния, нищо не е качено,
  входът е махнат. Нужно е човекът първо да влезе в dash.cloudflare.com с
  акаунта на сайта.

### А.4 Мярка: bench_all и bench_honesty

От последната мярка на лаптопа (`976221b`, 05.11) в main има 70 комита:
портата, /permissions, честността A–E, /undo, облакът.

```powershell
$py = "$HOME\AppData\Local\pipx\pipx\venvs\genesis-agent\Scripts\python.exe"
$env:GENESIS_START_PROVIDER = "claude_cli"
$b = "$HOME\.genesis\bench\all-20261105-183711"
& $py scripts\bench_all.py --quick --baseline-projects "$b\projects\results.json" `
  --baseline-fix "$b\fix\results.json" --baseline-fcc "$b\fcc\results.json"
& $py scripts\bench_honesty.py --ref 976221b     # очаквано 15/15 хванати, 0/12 фалшиви
```
Базата е файлът `results.json` на всяка стъпка — папката се отказва
(„няма го или не става за база“). С `--dry-run` е проверено на 15.11.
- `--quick` трае ~5 ч (05.11: 4 ч 48 мин) и харчи абонамента — само с „да“ от
  човека, със затворени излишни програми (бенчът пада при малко RAM) и без
  пипане на кода, докато върви (пусни го от отделен `git worktree`).
- Базата 05.11: projects **54/54**, fix **13/13**, fcc **30/30**. Регресия →
  спри и покажи проекта/задачата и защо.

## Б. Само човекът (агентът води и записва, не пуска вместо него)

### Б.1 Сървърът
1. Наемане: Ubuntu 22.04/24.04, Docker, ≥ 4 GB RAM, в ЕС. Сравнението от 13.11 е
   в бележката на оператора (не в репото).
2. Инсталация и деплой: `cloud/README.md` → „На истинския сървър“
   (`DEPLOY_DRY_RUN=1 ./cloud/deploy.sh`, после `sudo GENESIS_DOMAIN=… ./cloud/deploy.sh`).
3. `/srv/genesis/web.args` — **с `--keep-days 90`** (#123; колкото обещава
   `СРОК_РАЗГОВОРИ` в `legal.json`), пример в `cloud/README.md` → „Stripe“.
   Без него нищо не се трие, а поверителността обещава срок.
4. `/srv/genesis/legal.json` — полетата от `cloud/web/legal.py` (`FIELDS`);
   `"ПРЕГЛЕДАНО": "да"` само след юрист.
5. На сървъра, не тук:
   - качване на 25 MiB файл зад истинския Caddy (`26MiB` в `deploy/Caddyfile`,
     #120) и отказ при 27 MiB;
   - `bench_fix` в контейнера, очаквано 12/12 (`cloud/README.md` → „Проверка и мярка“);
   - разговор без ход от N дни изчезва след прохода (файлове на uid 10001).

### Б.2 Stripe live
Стъпките — `cloud/README.md` → „Stripe“. Накратко: активиране (физическо лице),
продукти и цени точно като в `prices.json` на сървъра, ограничен ключ `rk_live_`
(Checkout Sessions: Write, Prices: Read, Subscriptions: Write — не е пробван),
webhook със седемте събития, `stripe.env` (chmod 600), после:
```bash
stripe prices list --live --limit 100 > stripe-prices.json
sudo python3 -m cloud.golive_check --stripe-prices stripe-prices.json   # → ГОТОВО ЗА LIVE
```
Първото истинско плащане — p5 със собствена карта, после връщане от таблото
(кредитите трябва да изчезнат). Цените (`cloud/PRICING.md`) чакат „да / промени“.
Данъци/ДДС — със счетоводител преди първия клиент.

### Б.3 Телефонът
```powershell
$py = "$HOME\AppData\Local\pipx\pipx\venvs\genesis-agent\Scripts\python.exe"
cd $HOME\Projects\genesis-agent         # папката, в която Genesis работи от телефона
& $py -m genesis_agent.cli serve        # QR код → Genesis Remote на телефона
```
`serve --reset` само за нов ключ; извън дома `--host <Tailscale адрес>`;
iPhone без приложение `--web`. Защитната стена: „Частни мрежи“.

### Б.4 Наживо (от „На лаптопа — един промпт“ в NEXT_STEPS.md, част Б)
`/listen` с микрофона; гласовете; Desktop — микрофон и поставяне на снимка.

## Готов промпт

Постави го в Claude Code в checkout-а на genesis-agent на лаптопа:

```text
Работиш в checkout-а на genesis-agent на лаптопа (Windows, ключовете са в
~/.genesis). Прочети CLAUDE.md и LAPTOP_TASKS.md — вярвай им, не одитирай репото.

1. git fetch origin; git checkout main; git pull.
2. Част А по ред, без зачеркнатите. А.4 (bench, ~5 ч, харчи абонамента) — първо
   ме питай. А.3 (сайтът) — преди качване покажи `wrangler whoami` и
   `pages project list`; ако домейнът не е genesis-waitlist.pages.dev без
   наставка, НЕ качвай, а ме помоли да вляза в Cloudflare с акаунта на сайта.
3. Част Б — води ме стъпка по стъпка и записвай какво казвам; не пускай вместо мен.
4. Накрая: зачеркни свършеното в LAPTOP_TASKS.md с датата и числата, commit в нов
   клон claude/laptop-<дата>, push и PR към main (не сливай). Регресия → issue.
Не пипай код без тест, който пада без поправката.
```
