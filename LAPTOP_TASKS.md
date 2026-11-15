# За лаптопа — отвореното след 2026-11-15

Каквото остана след PR #120, #122 и #123 (всички слети в main на 15.11). Тук са само
нещата, които искат лаптопа, ключовете, сървъра или твое решение. Свършеното и
измереното е в `NEXT_STEPS.md` (разделите от 15.11, 13.11 и 12.11).

**Как:** в checkout-а на genesis-agent на лаптопа пусни Claude Code (или `genesis`) и
постави блока по-долу. Агентът прави сам А и Б, а за В те води стъпка по стъпка.

```text
Работиш в checkout-а на genesis-agent на лаптопа (Windows; истинските ключове са в
~/.genesis). git fetch origin; git checkout main; git pull. Прочети CLAUDE.md,
LAPTOP_TASKS.md и в NEXT_STEPS.md разделите от 15.11, 13.11 и 12.11. Не одитирай
наново цялото репо. Работи в нов клон claude/laptop-20261115.

А. Код — прави сам. Всяка поправка с тест, който пада без нея; ruff, mypy и pytest
   по CLAUDE.md преди всеки commit; коментарите на български, с дата и защо.
1. Паролата в облака (cloud/web/server.py, cloud/web/store.py):
   - „Сметка“ → смяна на паролата: старата + новата, същите правила като при
     регистрацията; след смяна другите сесии се затварят, текущата остава.
     Грешна стара парола се брои към заключването на входа (като /api/login).
   - Командата `admin reset-password <имейл>`: нова случайна парола, отпечатана
     като при add-user; всички сесии на човека се затварят. Спрян или изтрит
     (deleted-…@invalid) профил — отказ.
   - Възстановяване по имейл: само ако ме попиташ и кажа „да“ — иска SMTP
     (виж как го прави cloud/monitor.py с MONITOR_SMTP_*). Без SMTP — само горните две.
2. ДДС в Stripe Checkout: първо ме питай дали съм регистриран по ДДС. Ако не —
   запиши го в NEXT_STEPS и не пипай кода. Ако да — в billing.create_checkout
   automatic_tax[enabled]=true и billing_address_collection=required зад нов флаг
   на serve (--stripe-tax), тест на полетата на формата, ред в cloud/golive_check.py
   и в cloud/README.md (Stripe Tax се включва и в таблото на Stripe — кажи ми как).

Б. Сам, с ключовете — запиши числата в NEXT_STEPS.md:
3. Колко са записаните за ранен достъп (само броя, без имейлите):
   GET https://genesis-waitlist.pages.dev/api/waitlist с
   Authorization: Bearer <GENESIS_WAITLIST_TOKEN от ~/.genesis/.env> → поле count.
4. Сайтът: cd site && npx wrangler pages deploy public --project-name genesis-waitlist
   --branch main (след `npx wrangler login`, ако иска). Отвори страницата и провери,
   че примерите са тези с файл на входа (PDF фактури и т.н.).

В. Води ме стъпка по стъпка. Не ги пускай вместо мен; записвай какво казвам.
5. Сървърът (SSH):
   - в /srv/genesis/web.args добави `--keep-days 90` (или колкото дни пише в
     СРОК_РАЗГОВОРИ в legal.json — трябва да съвпада); после в checkout-а на
     сървъра `sudo GENESIS_DOMAIN=<домейнът> ./cloud/deploy.sh` (сам прави git pull,
     проверява Caddyfile и рестартира; първо може с DEPLOY_DRY_RUN=1);
   - sudo python3 -m cloud.golive_check --stripe-prices stripe-prices.json →
     какво още липсва;
   - в журнала (journalctl -u genesis-web) след старта не трябва да има
     „срок: … не се изтри“.
6. Качване зад истинския Caddy (тук не е мерено): на Windows
   `fsutil file createnew a.bin 26000001` и `fsutil file createnew b.bin 26214400`;
   качи двата от чата → и двата трябва да минат (преди поправката Caddy спираше a.bin).
   `fsutil file createnew c.bin 26214401` → страницата казва „над 25 MB е“, без да го праща.
7. Мярката на веригата, която ще се продава (54/54 е мерено само с claude_cli):
   на сървъра командата от cloud/README.md → „Проверка и мярка“ (bench_fix.py в
   контейнера, keys.env с GATEWAY_MODELS от cloud/PRICING.md). Очаквано 12/12.
   По-малко → покажи ми кои задачи падат и защо, преди да продаваме.
8. Решения — покажи ми числата и чакай отговор:
   - цените: cloud/PRICING.md и т. 2 в NEXT_STEPS (12.11) — планове с включени задачи?
   - genesis.exe е неподписан (Smart App Control го спира) и го няма в PyPI:
     сертификат за подпис или Azure Trusted Signing; PyPI с trusted publishing от CI.
     Кажи ми цената и стъпките; нищо не купувай и не публикувай без мен.

Накрая: обнови NEXT_STEPS.md накратко — зачеркни свършеното, запиши числата и
решенията; commit в claude/laptop-20261115, push, PR към main; при зелен CI — слей.
```

## Какво вече е направено (за да не се прави наново)

- Качване на файлове в уеб чата (#120); Caddy вече е `26MiB`, не `26MB`.
- Срок на разговорите (#123): `serve --keep-days N`, тестове в `tests/test_cloud_retention.py`.
- Refund, оспорени плащания, отказ от абонамент, правни страници, `forget` (#121).
- Портата, /permissions, /undo, шлюзът, redact, честността A–E (#122).
