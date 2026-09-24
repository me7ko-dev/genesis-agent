# genesis-waitlist.pages.dev — страница за ранен достъп (етап 0)

Цел: да измерим дали има купувачи, преди да строим сървъра и плащанията.
Мярка: брой записани и какво искат да автоматизират (полето „need").

- `public/index.html` — страницата (без външни зависимости). Само `public/` се
  качва — README и `wrangler.toml` не стават публични адреси.
- `functions/api/waitlist.js` — Cloudflare Pages Function, пази записите в KV.
- `functions/api/waitlist.test.mjs` — `node --test site/functions/api/waitlist.test.mjs`.
- `wrangler.toml` — проектът `genesis-waitlist` и връзката `WAITLIST` към KV.

## Пуснато (2026-09-25)

Проект `genesis-waitlist` в Cloudflare Pages (https://genesis-waitlist.pages.dev),
KV `genesis-waitlist`, тайна `ADMIN_TOKEN` (копие: `GENESIS_WAITLIST_TOKEN` в
`~/.genesis/.env` на лаптопа). Проверено отвън: страницата 200, запис → `ok`,
списък без/с грешен токен → 403, с токена → записите; кирилицата се пази.

Качване на нова версия (след `npx wrangler login`):

```bash
cd site && npx wrangler pages deploy public --project-name genesis-waitlist --branch main
```

Адресът е `genesis-waitlist.pages.dev` — решение на оператора (2026-09-25): без
собствен домейн, metko.uk е в друг Cloudflare акаунт. Работи изцяло на
Cloudflare, лаптопът и homeserver-ът не участват.

## Колко са записаните

```bash
curl -H "Authorization: Bearer <ADMIN_TOKEN>" https://genesis-waitlist.pages.dev/api/waitlist
```

Връща `count` и всички записи (имейл, какво искат, дата, държава).
