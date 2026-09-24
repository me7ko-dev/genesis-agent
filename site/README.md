# genesis.metko.uk — страница за ранен достъп (етап 0)

Цел: да измерим дали има купувачи, преди да строим сървъра и плащанията.
Мярка: брой записани и какво искат да автоматизират (полето „need").

- `index.html` — страницата (без външни зависимости).
- `functions/api/waitlist.js` — Cloudflare Pages Function, пази записите в KV.
- `functions/api/waitlist.test.mjs` — `node --test site/functions/api/waitlist.test.mjs`.

## Пускане (Cloudflare, ~10 мин, безплатно; homeserver-ът не се пипа)

1. **KV:** Cloudflare → Storage & Databases → KV → Create → име `genesis-waitlist`.
2. **Pages:** Workers & Pages → Create → Pages → Connect to Git → `me7ko-dev/genesis-agent`.
   - Production branch: `main`
   - Framework preset: `None`, Build command: празно
   - Root directory (Advanced): `site`
   - Build output directory: `.`
3. **Връзки:** проектът → Settings → Bindings → Add → KV namespace:
   име на променливата `WAITLIST` → `genesis-waitlist`.
4. **Таен ключ:** Settings → Variables and Secrets → Add → Secret:
   `ADMIN_TOKEN` = дълга случайна парола (тя пази списъка).
5. **Домейн:** проектът → Custom domains → `genesis.metko.uk` (DNS е в Cloudflare → сам се връзва).
6. Deployments → Retry deployment, за да хване връзките от т. 3–4.

## Колко са записаните

```bash
curl -H "Authorization: Bearer <ADMIN_TOKEN>" https://genesis.metko.uk/api/waitlist
```

Връща `count` и всички записи (имейл, какво искат, дата, държава).
