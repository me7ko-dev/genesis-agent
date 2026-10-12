# Genesis онлайн — всяка задача в затворен контейнер

```
браузър ─► Caddy (HTTPS) ─► web/server.py ─► launch.py ─► docker run genesis-runner  ◄── само /work
                                                              │  своя мрежа genesis-job-<id> (--internal)
                                                              ▼
                                       genesis-gateway :8090 ─► API на моделите (ключовете са тук)
                                    (или genesis-egress :3128 ─► само API на моделите, порт 443)
```

Всяка задача е в своя вътрешна мрежа, в която е само помощникът ѝ (шлюзът,
или без шлюз — проксито). Другите задачи не се виждат, навън няма път.

| Файл | Какво прави |
|---|---|
| `runner/task.py` | в контейнера: задача → агентният цикъл → JSON редове; последният е `done` с токени и секунди |
| `runner/launch.py` | на сървъра: мрежа за задачата, `docker run` с всички тавани, таймаут, събиране на събитията |
| `egress/proxy.py` | изходът в режим с прокси: `CONNECT` само към API на модели, порт 443 |
| `gateway/gateway.py` | режим с шлюз: ключовете остават тук, задачата носи жетон; сметката |
| `runner/task-requirements.txt` | библиотеките в образа (Excel, PDF, Word, снимки, pandas, pytest) — PyPI е затворен |
| `build.sh`, `up.sh` | строене на образите; мрежите и помощникът (шлюз или прокси) |
| `deploy.sh`, `deploy/` | деплой с една команда; systemd, Caddyfile, daemon.json |
| `check.py` | готова ли е машината — по истинския път на задача |
| `backup.py` | копие на web.db и usage.jsonl, докато сървърът работи |
| `monitor.py` | наблюдение на 5 мин и аларма (имейл/webhook) само при смяна |
| `web/server.py` | вход, уеб чат, сваляне на файловете (само stdlib) |
| `web/store.py` | SQLite: хора (scrypt), сесии (SHA-256 на жетона), разговори, ходове, сметка, покани |
| `web/billing.py`, `web/prices.json` | етап 3: цени, такса на ход от шлюза, Stripe (само stdlib) |

## Измерено тук (2026-10-11, Docker 29.8.2, cgroup v1, без API ключове)

| Проверка | Резултат |
|---|---|
| Направо навън от задача | няма изход — „Network is unreachable“, дори DNS не работи |
| Друга задача (по име и по IP) | недостъпна — името не се резолвира, IP-то е „unreachable“ |
| Шлюзът от мрежата на задачата | 200; проксито (режим с прокси) — 403 за `example.com` |
| 1 GB памет при таван 256 MB | убито, код 137 |
| 500 процеса при таван 64 | спряно на 62 |
| Запис извън `/work` | отказан (read-only) |
| Потребител | 10001, без root, без capabilities, no_new_privs |
| Задача без работещ модел | `ok: false` с грешката — не се таксува като успех (и в режим с шлюз) |
| SIGTERM на уеб сървъра посред задача | контейнерът и мрежата му изчезват за 0.6 s; ходът е „сървърът спря…“ |
| Втори `serve` на зает порт | излиза с 1, не пипа ходовете и контейнерите на живия |
| Мрежа за задача (create+connect+rm) | ~1.2 s на задача |
| Сметката от usage.jsonl с 1M реда (200 MB) | 0.13 ms (преди: 3.55 s на всеки ход) |
| Образи | runner 590 MB, egress 177 MB, gateway 177 MB; build.sh — 46 s |

Изхода, помощника, таваните, правата и задачата без модел проверява
`sudo python3 -m cloud.check` на машината.

## На истинския сървър (Ubuntu 22.04/24.04)

```bash
sudo apt install -y docker.io python3 python3-pip git caddy   # Caddy: виж caddyserver.com/docs/install
# кодът е на root: уеб сървърът върви като root от тази папка (deploy.sh отказва чужда)
sudo git clone https://github.com/me7ko-dev/genesis-agent /opt/genesis && cd /opt/genesis
sudo mkdir -p /srv/genesis
# ключовете остават в шлюза; GATEWAY_SECRET — поне 32 знака
printf 'GATEWAY_SECRET=%s\nGROQ_API_KEY=...\nNVIDIA_API_KEY=...\n' "$(openssl rand -base64 48)" \
  | sudo tee /srv/genesis/keys.env >/dev/null && sudo chmod 600 /srv/genesis/keys.env
DEPLOY_DRY_RUN=1 ./cloud/deploy.sh                      # само показва стъпките
sudo GENESIS_DOMAIN=genesis.example.com ./cloud/deploy.sh
sudo python3 -m cloud.web.server add-user ivan@example.com   # бета: хората се добавят ръчно
```

`deploy.sh` е идемпотентен и спира на първата грешка: проверки (root, Docker,
python3 ≥ 3.10 и pip, кодът само на root и без o+w, GATEWAY_SECRET, права на
keys.env) → `git pull --ff-only`
→ `build.sh` (етикети `:latest` и `:<git sha>`) → `/etc/docker/daemon.json`, ако
го няма → `up.sh` → systemd → Caddy → `cloud/check.py` → `https://<домейн>/healthz`.

- **Режимът** идва от keys.env: с `GATEWAY_SECRET` — шлюз (ключовете не влизат в
  контейнера, проксито не се пуска); без него — прокси и `--env-file` в контейнера.
- **systemd:** `genesis-net` (up.sh при всяко пускане; ако помощникът не е здрав,
  уеб сървърът не тръгва), `genesis-web` (root: вика docker и дава папките на
  uid 10001; `UMask=0077`; при спиране убива задачите си, `ExecStopPost` чисти
  каквото е останало), `genesis-backup.timer` (на 6 часа), `genesis-monitor.timer`
  (на 5 минути — виж „Наблюдение и аларми“). Логовете на уеб
  сървъра са в journald (`journalctl -u genesis-web`; таван — `SystemMaxUse=`
  в journald.conf). Зад Caddy там се вижда само 127.0.0.1 — истинският адрес е в
  `/var/log/caddy/genesis.log`.
- **Caddy:** `deploy/Caddyfile` — HSTS, gzip, тяло ≤ 1 MB, лог с ротация. Уеб
  сървърът е с `--trust-proxy` (последният адрес в `X-Forwarded-For`), никога с
  `--trust-cloudflare` (зад Caddy клиентът сам пише `CF-Connecting-IP`).
- **daemon.json:** логове 5 × 10 MB и мрежи по /24 от 10.200.0.0/16 (по
  подразбиране Docker има място за ~30 мрежи на задачи). Ако файлът вече има
  нещо, `deploy.sh` не го пипа, а казва какво липсва. Важи за нови контейнери:
  след промяна — `systemctl restart docker && ./cloud/up.sh`.
- **Защитна стена:** само 22, 80, 443 (`ufw allow OpenSSH; ufw allow 80,443/tcp;
  ufw enable`). Docker не публикува портове, затова заобикалянето на ufw тук не важи.
- **gVisor (по-силна изолация):** инсталирай runsc и `GENESIS_RUNTIME=runsc ./cloud/deploy.sh`.
  Тук не е мерено (няма runsc).
- **Връщане назад:** `docker tag genesis-runner:<стар sha> genesis-runner:latest`
  (същото за egress/gateway), после `./cloud/up.sh`.

### Проверка и мярка

```bash
sudo python3 -m cloud.check --web http://127.0.0.1:8080     # код ≠ 0 при първия проблем
# мярката за етап 1 — очаквано 12/12, както на лаптопа (режим с прокси, ключове в контейнера):
docker run --rm --network genesis-jobs --env-file /srv/genesis/keys.env \
  -e HTTPS_PROXY=http://genesis-egress:3128 -e NO_PROXY=localhost,127.0.0.1 \
  --entrypoint python genesis-runner:latest /opt/bench_fix.py
# една задача на ръка:
python3 -m cloud.runner.launch --workspace /srv/jobs/1 --env-file /srv/genesis/keys.env \
  --gateway-usage /srv/genesis/gateway/usage.jsonl "направи CSV с числата от 1 до 10 и техните квадрати"
```

`genesis-jobs` е само за такива ръчни проверки: в нея е помощникът на режима
(шлюзът или проксито). Задачите от `launch.py` са всяка в своя `genesis-job-<id>`.

### Данни: копия, ротация, задържане

- **Копие:** `genesis-backup.timer` → `python3 -m cloud.backup` → `/srv/backup/genesis/<време>/`
  (web.db през онлайн backup API + `integrity_check`, копие на usage.jsonl, 14 дни,
  права само за root). Извън машината — разкоментирай `ExecStartPost=rsync …` в
  `genesis-backup.service` (Storage Box и SSH ключ — решение на оператора).
- **Възстановяване:** `systemctl stop genesis-web`, копирай `web.db` от копието в
  `/srv/genesis/web/`, `systemctl start genesis-web`, влез.
- **usage.jsonl** е единственият запис за сметката — не се ротира с logrotate
  (шлюзът зарежда бюджетите от него при старт; при 1M реда — 5 s). Веднъж в
  месеца, без вървящи задачи: `systemctl stop genesis-web; docker stop genesis-gateway;
  mv usage.jsonl usage-<месец>.jsonl; docker start genesis-gateway; systemctl start genesis-web`.
- **Не се трие автоматично:** папките на разговорите в `/srv/jobs` и събитията
  в web.db (до 8 MiB на ход). Колко дълго се пазят е решение на оператора —
  `check.py` пада при под 5 GB свободни (`--min-free-gb`).

### Наблюдение и аларми (2026-10-12)

`genesis-monitor.timer` пуска `python3 -m cloud.monitor --once` на 5 минути. Аларма
се праща **само при смяна** (наред → проблем и проблем → наред), всички промени от
едно пускане — в едно съобщение. Състоянието е в `/srv/genesis/web/monitor-state.json`.

| Проверка | Проблем е, когато |
|---|---|
| уеб сървърът | `/healthz` не е 200 (`check.web_health`) |
| шлюзът/проксито | не е Up или се рестартира (`check.helper`) |
| диск | под `MONITOR_MIN_FREE_GB` (5) за `/srv/jobs` или `/srv/genesis/web` |
| провалени ходове | над `MONITOR_MAX_FAILED_TURNS` (10) за последния час |
| разход | таксувани кредити за последния час над `MONITOR_MAX_USER_CREDITS_HOUR` (200) на човек или `MONITOR_MAX_TOTAL_CREDITS_HOUR` (1000) общо; човекът е с номера си (`users`), не с имейла |
| заключени входове | над `MONITOR_MAX_LOCKOUTS_HOUR` (20) за последния час — по един на имейл/IP/покана на 15 мин (таблица `lockouts` в web.db) |
| висящи задачи | контейнер `genesis-task-*` по-стар от `MONITOR_TASK_SECONDS` (600, като `--seconds` на сървъра) + 2 мин |
| резервно копие | последното е по-старо от `MONITOR_MAX_BACKUP_HOURS` (13) или няма нито едно |

```bash
sudo install -m 600 cloud/deploy/monitor.env.example /srv/genesis/monitor.env
sudo nano /srv/genesis/monitor.env              # webhook (Slack/Discord/Telegram) и/или SMTP
sudo python3 -m cloud.monitor --once --dry-run  # всички проверки + какво би пратил; нищо не праща
sudo systemctl start genesis-monitor.service    # едно истинско пускане
journalctl -u genesis-monitor -n 30             # какво е видял и пратил
systemctl list-timers 'genesis-*'
```

- **Канали:** `MONITOR_WEBHOOK_URL` с `MONITOR_WEBHOOK_FORMAT` `text` (Slack,
  Mattermost), `discord` или `telegram` (+ `MONITOR_TELEGRAM_CHAT_ID`, адресът е
  `https://api.telegram.org/bot<жетон>/sendMessage`); имейл — `MONITOR_SMTP_HOST`,
  `_PORT`, `_TLS` (starttls/ssl/none), `_USER`, `_PASSWORD`, `_FROM`, `_TO`. Може и
  двата. Без `monitor.env` алармите са само в журнала.
- **Тайните:** `deploy.sh` иска файла `600` и го проверява (грешна стойност спира
  деплоя). Паролата и адресът на webhook-а не се печатат — в грешките са `***`;
  `--dry-run` показва само хоста.
- **Паднал канал:** състоянието не се записва и `genesis-monitor.service` е
  `failed` (`systemctl --failed`) — следващото пускане праща пак. При два канала
  и един паднал работещият получава съобщението повторно.
- **Висящ Docker:** всяко `docker` от наблюдението е с таван 15 с, пускането —
  с `TimeoutStartSec=300`; алармата „шлюзът/проксито“ и „висящи задачи“ тръгва
  и когато демонът не отговаря.
- **Нова машина:** до първото копие (таймерът е на 6 ч) „резервно копие“ е
  проблем — `sudo systemctl start genesis-backup.service` веднъж след деплоя.
- Проверено (2026-10-12): `tests/test_cloud_monitor.py` — фалшиви Docker, SMTP,
  webhook и часовник; `systemd-analyze verify` на единиците. С истински SMTP и
  Telegram не е мерено (тук няма такива).

## Етап 2: вход, уеб чат, файлове

На ръка (без systemd) — същото, което пуска `genesis-web.service`:

```bash
python3 -m cloud.web.server add-user ivan@example.com      # печата парола
python3 -m cloud.web.server serve --data /srv/genesis/web --jobs /srv/jobs \
  --env-file /srv/genesis/keys.env --gateway-usage /srv/genesis/gateway/usage.jsonl --trust-proxy
```

Зад Cloudflare Tunnel — `--trust-cloudflare` (IP от `CF-Connecting-IP`); зад Caddy/nginx —
`--trust-proxy` (последният адрес в `X-Forwarded-For`). Никога и двете: зад Caddy клиентът
може сам да напише `CF-Connecting-IP` и да заобиколи ограничението на опитите за вход.

Разговорът е една папка; всяко съобщение е нов контейнер върху нея, историята
(само въпроси и крайни отговори, ≤20 съобщения / 12k знака) е в `/work/.genesis`.
По един ход на човек, общо `--workers` (2) контейнера. Файловете от контейнера
са недоверени: сваля се само като прикачен файл, без символни връзки, без `..`,
и само между ходовете. Чуждите разговори и файлове връщат 404. 5 грешни пароли
за 15 мин → 429.

При старт сървърът първо заема порта, после убива останалите контейнери на
задачи и маха мрежите им, и чак тогава маркира прекъснатите ходове като
неуспешни. При SIGTERM спира текущите задачи. Освен това контейнерът сам спира
30 s след своя таван (`GENESIS_TASK_DEADLINE`), ако никой друг не го спре.

## Шлюз за моделите (`gateway/`): ключовете не влизат в контейнера

```
задача (без изход навън) ──жетон──► genesis-gateway:8090 ──истински ключ──► API на модела
                                         └─► /srv/genesis/gateway/usage.jsonl (сметката)
```

В `keys.env` добави `GATEWAY_SECRET=` (поне 32 знака, напр. `openssl rand -base64 48`);
`./cloud/up.sh` пуска шлюза вместо проксито и проверява, че е жив и отговаря
(иначе код 1 и последните редове от лога му). Като root прави keys.env
`root:10003 0640` — шлюзът (uid/gid 10003) го чете през групата, уеб сървърът като собственик.

Контейнерът получава само `GENESIS_MODEL_GATEWAY` и подписан жетон
(задача + срок = таймаутът + 1 мин + бюджет, по подразбиране 300 000 токена)
вместо всеки ключ; прокси и `--env-file` няма. Шлюзът приема само
`POST /<доставчик>/chat/completions`, слага ключа (ротира `_2`.. при 429/401),
брои `usage` и при изчерпан бюджет връща 429. Сметката се чете от шлюза, не
от `done` събитието на контейнера — само редовете след старта на задачата.

Измерено (2026-09-25, лаптоп, истински ключове само в шлюза): задача „направи
squares.csv …" → готова за 3.2 s, 2 обръщения, 5 536 токена; шлюзът и задачата
отчитат едно и също до токен. Тестове: `tests/test_cloud_gateway.py` (подправен
жетон, изтекъл, чужда задача, бюджет, ротация, ключове извън `docker run`).

## Етап 3: плащане и бета

Кредити: 100 кредита = 1 EUR, вътрешно цели микрокредити (µc). Ходът струва
`per_turn` + токените по тарифата на модела, а токените са **само от
`usage.jsonl` на шлюза** — затова `--billing` без `--gateway-usage` не тръгва.
Сметката е таблица само с добавяне (`ledger`): балансът е сбор, всеки запис има
ключ (`turn:<id>`, `stripe:<cs_|in_>`, `admin:<uuid>`, `invite:…`) и не се
записва два пъти; поправка е нов ред, никога редакция.

```bash
python -m cloud.web.server serve ... --env-file /srv/genesis/keys.env \
  --gateway-usage /srv/genesis/gateway/usage.jsonl --billing \
  --public-url https://genesis.example.com      # нужен за Stripe (или GENESIS_PUBLIC_URL)
```

| Флаг | По подразбиране | Какво |
|---|---|---|
| `--billing` | изключено | кредити; иска `--gateway-usage` |
| `--prices PATH` | `cloud/web/prices.json` | тарифите (JSON); невалиден файл → изход 2 |
| `--turns-per-day N` | 50 | ходове на човек на ден (UTC) — **важи и без плащане** |
| `--max-tokens-per-turn N` | 300 000 | таван на токените на ход в шлюза |
| `--monthly-cap CREDITS` | 2000 | месечен таван на харченето на човек |

Преди ход (под заключване): спрян профил → 403, вече върви ход → 409, лимитът
за деня → 429, месечният таван или балансът → 402 („Зареди от „Сметка““).
Таванът на токените в жетона на задачата става колкото балансът плаща по
най-скъпата тарифа (`default`), така че надхарчването е най-много грешката в
оценката на шлюза за една заявка. Провален ход също се таксува — шлюзът е
платил токените му. Таксата е преди края на хода (дотогава човекът е зает).
Срив между края на хода и таксата: при старт сървърът таксува пропуснатото
(и `reconcile` от командния ред) — пак само веднъж. Ход, отказан при спиране
на сървъра, преди контейнерът му да тръгне, не се таксува (жетон няма).

Жетонът на задачата живее `limits.seconds + 60` s и след края на контейнера
(кодът в него може да го запише в `/work` за следващия ход). Затова сметката
на хода е окончателна чак ~17 мин след като жетонът е направен (жетонът +
300 s за последната заявка + 1 мин; от истинското му изтичане — мрежата на
задачата преди него може да отнеме до 2 мин при бавен docker демон): тогава
разходът след таксата се таксува като
`turn:<id>:late`. Дотогава неизхарченото от бюджета на жетона е **задържано**
от баланса — малък баланс стига за един-два хода на всеки ~17 мин; по-малък
`--max-tokens-per-turn` значи по-малко задържано.

**Цените в `prices.json` са консервативен пример, не решение** — провери ги
срещу ценоразписа на доставчиците. Правила, които файлът налага: всяка тарифа
е цяло число ≥ 0 (кредити на 1M токена); `default` е най-скъпата по всяко поле
(името на модела идва от кода в контейнера — непознато име пада към `default`
и не бива да е по-евтино); ключовете са `доставчик/модел` или `доставчик/*` с
доставчик от шлюза. Моделите, които шлюзът пуска, се изброяват в ключовете:
`GATEWAY_MODELS=groq/*,openai/gpt-4o-mini` (`доставчик/модел` или `доставчик/*`).
Без списък шлюзът пуска всеки модел, който кодът в контейнера поиска, затова
`--billing` не тръгва без него, нито ако някой от изброените няма свой ред в
`models` (иначе щеше да се таксува по `default`). `доставчик/*` в списъка
пуска **всеки** модел на доставчика — тогава цената му трябва да покрива най-
скъпия (при openai и openrouter изброявай моделите поотделно).
Измерената задача (5 536 токена) по `default` е ≈ 3.7 кредита ≈ 0.04 EUR.

### Stripe (по желание)

Ключовете **само от средата** на процеса (не флагове, не `prices.json`):
`STRIPE_SECRET_KEY` (`sk_…`/`rk_…`) и `STRIPE_WEBHOOK_SECRET` (`whsec_…`).
Нито един → картите са изключени (зареждане само с `grant`); само единият → сървърът
не тръгва — половин настройка взема пари, без да зарежда кредити. С `sk_test_`
тестови събития, с `sk_live_` — истински; другите се пропускат.

В таблото на Stripe:
1. Продукти и цени (Price) за пакетите и плана; `price_…` в `stripe_price` в `prices.json`.
2. Webhook към `https://<адрес>/api/stripe/webhook` със събития
   `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `invoice.paid`, `customer.subscription.deleted`; тайната му е `STRIPE_WEBHOOK_SECRET`.

Webhook-ът проверява подписа (HMAC-SHA256 върху суровите байтове, ±5 мин)
преди всичко друго; зарежда само ако сумата и валутата съвпадат с пакета/плана
и само веднъж за сесия или фактура. Фактура преди връзката с клиента → 503
и Stripe опитва пак. Всеки абонамент е нов Stripe клиент (`customer_email`):
два платени абонамента на един човек зареждат и двата, спирането на единия
не спира другия. Адресите за връщане са от `--public-url`, не от `Host`.

### Командите на оператора (само локално — няма админ адрес в уеба)

```bash
S="python -m cloud.web.server"; D="--data /srv/genesis/web"
$S users $D                                   # баланс, похарчено този месец, днес, план
$S balance ana@example.com $D                 # баланс и последните 20 реда
$S grant ana@example.com 12.5 $D --note бета  # кредити; отрицателно = корекция
$S disable ana@example.com $D                 # спира и затваря сесиите; enable го връща
$S limits ana@example.com $D --turns-per-day 10 --max-tokens 100000 --monthly-cap 500
$S limits ana@example.com $D --clear          # обратно към подразбирането
$S invite $D --count 10 --credits 50 --public-url https://genesis.example.com
$S reconcile $D --gateway-usage /srv/genesis/gateway/usage.jsonl
```

Покана: еднократен код (в базата е SHA-256), връзката `https://<адрес>/?invite=<код>`
отваря „Регистрация с покана“. Грешни кодове се броят като грешни пароли
(5 за 15 мин на IP → 429). „Сметка“ в страницата показва баланса, месеца,
задачите днес, пакетите и историята; след Stripe (`?view=account&paid=1`)
чете сметката на 3 s до 10 пъти, докато дойде webhook-ът.

Проверено (2026-10-11): `tests/test_cloud_billing.py` — сметка само с
добавяне, покани (10 паралелни с един код → 1 човек), цени, такса от шлюза
(контейнерът казва 0 → таксува се шлюзът), рестарт посред хода, 402/429,
20 паралелни хода → 1, подпис и webhook на Stripe, Checkout с фалшив Stripe,
командите. Страницата „Сметка“ още **не е проверена в браузър** (тук няма
такъв) — само `node --check` и HTTP тестовете.

Известни пропуски (бетата е с 10 души и ръчно раздадени покани):
- няма потвърждение на имейла;
- връщане на пари и chargeback не се отразяват сами — `grant … -X` ръчно;
- `usage.jsonl` се чете от мястото, до което стигаше преди хода (записано в
  `turn_jobs.usage_start`); `reconcile` и окончателната сметка — от най-ранното
  такова място сред чакащите ходове;
- денят и месецът са по UTC;
- окончателната сметка (`turn:<id>:late`) тръгва покрай ход, при старт или с
  `reconcile` — без нов ход на никого тя чака;
- шлюзът чака отговор до 300 s на операция; доставчик, който праща тялото по
  байт, би записал разхода след окончателната сметка (не е виждано).

## Известно и отложено

- **PyPI е затворен.** Каквото липсва в `task-requirements.txt`, не може да се
  инсталира. Добавяй там и мери размера на образа.
- **Не е мерено тук:** bench_fix 12/12 (иска ключове и интернет), gVisor (няма
  runsc), истински systemd (контейнерът тук няма systemd като PID 1 — единиците
  минават `systemd-analyze verify`, командите им са пускани на ръка).
- **Задържане на данни** (`/srv/jobs`, събития в web.db) и цени — решения на оператора.
