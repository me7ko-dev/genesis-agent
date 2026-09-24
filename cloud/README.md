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

- **Ключовете са в контейнера.** Кодът на задачата може да ги прочете и да вика
  моделите директно (само тях — проксито не пуска другаде). Преди публичен
  старт (етап 4): шлюз за моделите на сървъра, който държи ключовете и мери
  токените на задача; контейнерът получава само еднократен жетон.
- **PyPI е затворен.** Каквото липсва в `task-requirements.txt`, не може да се
  инсталира. Добавяй там и мери размера на образа.
