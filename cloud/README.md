# Genesis онлайн — етап 1: всяка задача в затворен контейнер

```
браузър ─► (етап 2: сървър с вход) ─► launch.py ─► docker run genesis-runner  ◄── само /work
                                                        │  мрежа genesis-jobs (--internal, без изход)
                                                        ▼
                                                  genesis-egress :3128 ─► само API на моделите, порт 443
```

| Файл | Какво прави |
|---|---|
| `runner/task.py` | в контейнера: задача → агентният цикъл → JSON редове; последният е `done` с токени и секунди |
| `runner/launch.py` | на сървъра: `docker run` с всички тавани, таймаут, събиране на събитията |
| `egress/proxy.py` | единственият изход: `CONNECT` само към API на модели, порт 443 |
| `runner/task-requirements.txt` | библиотеките в образа (Excel, PDF, Word, снимки, pandas, pytest) — PyPI е затворен |
| `build.sh`, `up.sh` | строене на образите; мрежите и проксито |
| `web/server.py` | етап 2: вход, уеб чат, сваляне на файловете (само stdlib) |
| `web/store.py` | SQLite: хора (scrypt), сесии (SHA-256 на жетона), разговори, ходове |

## Измерено тук (2026-09-24, Docker 29, без API ключове)

| Проверка | Резултат |
|---|---|
| Направо навън от задача | няма изход — дори DNS не работи |
| През проксито към `example.com` | 403 |
| През проксито към `api.groq.com` | пуснато, тунелът е отворен |
| 1 GB памет при таван 256 MB | убито, код 137 |
| 500 процеса при таван 64 | спряно на 63 |
| Запис извън `/work` | отказан (read-only) |
| Потребител | 10001, без root, без capabilities |
| Таймаут 3 s | контейнерът е убит за 3.2 s, нищо не остава живо |
| Задача без работещ модел | `ok: false` с грешката — не се таксува като успех |
| Образи | runner 587 MB, egress 177 MB |

## На истинския сървър (Ubuntu, Docker)

```bash
git clone https://github.com/me7ko-dev/genesis-agent && cd genesis-agent
./cloud/build.sh          # ARM сървър: PLATFORM=manylinux2014_aarch64 ./cloud/build.sh
./cloud/up.sh
printf 'GROQ_API_KEY=...\nNVIDIA_API_KEY=...\n' > /srv/genesis/keys.env && chmod 600 /srv/genesis/keys.env

# мярката за етап 1 — очаквано 12/12, както на лаптопа:
docker run --rm --network genesis-jobs --env-file /srv/genesis/keys.env \
  -e HTTPS_PROXY=http://genesis-egress:3128 -e NO_PROXY=localhost,127.0.0.1 \
  --entrypoint python genesis-runner:latest /opt/bench_fix.py

# една задача:
python -m cloud.runner.launch --workspace /srv/jobs/1 --env-file /srv/genesis/keys.env \
  "направи CSV с числата от 1 до 10 и техните квадрати"
```

По-силна изолация: инсталирай gVisor и добави `--runtime runsc`.

## Етап 2: вход, уеб чат, файлове

```bash
python -m cloud.web.server add-user ivan@example.com      # бета: хората се добавят ръчно, печата парола
python -m cloud.web.server serve --data /srv/genesis/web --jobs /srv/jobs   --env-file /srv/genesis/keys.env --trust-proxy           # 127.0.0.1:8080, отпред Caddy/Cloudflare с HTTPS
```

Разговорът е една папка; всяко съобщение е нов контейнер върху нея, историята
(само въпроси и крайни отговори, ≤20 съобщения / 12k знака) е в `/work/.genesis`.
По един ход на човек, общо `--workers` (2) контейнера. Файловете от контейнера
са недоверени: сваля се само като прикачен файл, без символни връзки, без `..`,
и само между ходовете. Чуждите разговори и файлове връщат 404. 5 грешни пароли
за 15 мин → 429. Проверено: 40 теста (`tests/test_cloud_web.py`) и в браузър с
демо runner (вход, задача, събития, CSV и zip, телефон 375 px).

## Известно и отложено

- ~~Ключовете са в контейнера~~ — решено с шлюза (виж по-долу).
- **PyPI е затворен.** Каквото липсва в `task-requirements.txt`, не може да се
  инсталира. Добавяй там и мери размера на образа.

## Шлюз за моделите (`gateway/`): ключовете не влизат в контейнера

```
задача (без изход навън) ──жетон──► genesis-gateway:8090 ──истински ключ──► API на модела
                                         └─► /srv/genesis/gateway/usage.jsonl (сметката)
```

В `keys.env` добави `GATEWAY_SECRET=` (поне 32 знака, напр. `openssl rand -base64 48`),
`./cloud/up.sh` пуска шлюза, после:

```bash
python -m cloud.web.server serve ... --env-file /srv/genesis/keys.env   --gateway-usage /srv/genesis/gateway/usage.jsonl
```

Контейнерът получава само `GENESIS_MODEL_GATEWAY` и подписан жетон
(задача + срок = таймаутът + 1 мин + бюджет, по подразбиране 300 000 токена)
вместо всеки ключ; прокси и `--env-file` няма. Шлюзът приема само
`POST /<доставчик>/chat/completions`, слага ключа (ротира `_2`.. при 429/401),
брои `usage` и при изчерпан бюджет връща 429. Сметката се чете от шлюза, не
от `done` събитието на контейнера.

Измерено (2026-09-25, лаптоп, истински ключове само в шлюза): задача „направи
squares.csv …" → готова за 3.2 s, 2 обръщения, 5 536 токена; шлюзът и задачата
отчитат едно и също до токен. Тестове: `tests/test_cloud_gateway.py` (подправен
жетон, изтекъл, чужда задача, бюджет, ротация, ключове извън `docker run`).

