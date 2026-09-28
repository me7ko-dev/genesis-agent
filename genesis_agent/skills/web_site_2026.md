---
name: web_site_2026
category: web
min_score: 1
description: Уеб сайт или лендинг страница по стандарта на 2026 — проверена CSS основа
  (светла и тъмна тема с контраст AA), hero с тъмен слой, мобилно меню, достъпна
  форма, SVG илюстрации вместо заместители, цени в евро.
triggers:
- сайт
- уебсайт
- лендинг
- website
- landing
version: '1.0'
author: Genesis
last_updated: '2026-09-28T14:00:00+00:00'
---

## Описание
Проверено, не по памет. Измерено 2026-09-28 (задача „сайт за къща за гости“,
безплатната верига): бял текст върху светъл градиент 1.65:1, 17 нечетими текста
в тъмната тема, сива кутия „Карта (SVG placeholder)“, header на три реда, цени в
лева, og:image към example.com. Основата отдолу минава браузърната проверка
(Chromium 1440/390 px, светла и тъмна тема) без нито една находка —
tests/test_web_skill.py сглобява страница от нея и я проверява.

## Правила
- Цветове САМО от токените отдолу. Сменяш ли `--accent`, провери го с
  `contrast_ratio()` (Python блока): текст ≥ 4.5:1, едър текст и бутони ≥ 3:1 —
  и в двете теми.
- Текст върху снимка/SVG/градиент — само в `.hero` (там има тъмен слой).
- Нито една сива кутия или „placeholder“: илюстрации — SVG със слоеве и
  градиенти (планини, вълни, форми по темата); галерия — такива SVG сцени с
  подпис. Карта — iframe на OpenStreetMap с `marker=ширина,дължина`; не знаеш ли
  координатите — връзка „Отвори в картата“ (openstreetmap.org/search?query=…)
  и писмени указания как се стига.
- Цени в € (еврото е валута от 01.01.2026), конкретно съдържание на български —
  имена, часове, адрес, без „Lorem ipsum“.
- Точно един `<h1>`; `header`/`nav`/`main`/`footer`; `lang="bg"`,
  `<meta charset="utf-8">`, viewport, description; og:image с абсолютен адрес
  на истинския домейн или изобщо без него (не example.com).
- Бутони и връзки в менюто ≥ 44 px височина; видим фокус (`:focus-visible`).
- Анимациите — само с `prefers-reduced-motion: no-preference`; reveal чрез
  `animation-timeline: view()` (без JS, съдържанието никога не остава скрито).
- Без външни библиотеки; шрифтът — системният стек отдолу.
- Темата се помни в localStorage; скриптът от `<head>` я слага ПРЕДИ
  рисуването (без проблясване).

## CSS основа
```css
:root {
  color-scheme: light dark;
  --bg: light-dark(#faf8f5, #12100e);
  --surface: light-dark(#ffffff, #1c1917);
  --surface-2: light-dark(#f1ede6, #292524);
  --text: light-dark(#1c1917, #f5f5f4);
  --muted: light-dark(#57534e, #a8a29e);
  --border: light-dark(#e2dcd3, #3a3531);
  --accent: light-dark(#0f766e, #5eead4);
  --on-accent: light-dark(#ffffff, #042f2e);
  --radius: 1rem;
  --shadow: 0 1px 2px rgb(0 0 0 / .06), 0 8px 24px rgb(0 0 0 / .08);
  --font: "Inter", "Segoe UI Variable", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --step-0: clamp(1rem, .96rem + .2vw, 1.125rem);
  --step-2: clamp(1.5rem, 1.3rem + 1vw, 2.25rem);
  --step-4: clamp(2.25rem, 1.6rem + 3.2vw, 4.25rem);
}
:root[data-theme="light"] { color-scheme: light; }
:root[data-theme="dark"] { color-scheme: dark; }
*, *::before, *::after { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; background: var(--bg); color: var(--text); font: var(--step-0)/1.65 var(--font); }
img, svg { max-width: 100%; height: auto; display: block; }
h1, h2, h3 { line-height: 1.15; text-wrap: balance; margin: 0 0 .5em; }
h2 { font-size: var(--step-2); }
p { text-wrap: pretty; margin: 0 0 1em; }
a { color: var(--accent); }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 3px; border-radius: 4px; }
.container { width: min(1120px, 100% - 2.5rem); margin-inline: auto; }
.section { padding-block: clamp(3.5rem, 8vw, 6rem); }
.section--alt { background: var(--surface-2); }
.lead { color: var(--muted); font-size: 1.15em; max-width: 60ch; }
.skip-link { position: absolute; left: 1rem; top: -4rem; z-index: 100; padding: .75rem 1rem;
  background: var(--accent); color: var(--on-accent); border-radius: .5rem; }
.skip-link:focus { top: 1rem; }

.site-header { position: sticky; top: 0; z-index: 50; background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(12px); border-bottom: 1px solid var(--border); }
.site-header .container { display: flex; align-items: center; gap: 1rem; min-height: 4.25rem; }
.brand { font-weight: 800; font-size: 1.2rem; color: var(--text); text-decoration: none; margin-right: auto;
  display: inline-flex; align-items: center; gap: .5rem; min-height: 44px; }
.nav ul { display: flex; gap: .25rem; list-style: none; margin: 0; padding: 0; }
.nav a { display: block; padding: .7rem .8rem; border-radius: .6rem; color: var(--text); text-decoration: none; font-weight: 600; }
.nav a:hover { background: var(--surface-2); }
.icon-btn { inline-size: 44px; block-size: 44px; display: grid; place-items: center; border: 1px solid var(--border);
  border-radius: .75rem; background: var(--surface); color: var(--text); cursor: pointer; font-size: 1.1rem; }
.nav-toggle { display: none; }
@media (max-width: 820px) {
  .nav-toggle { display: grid; }
  .nav { position: absolute; inset: 100% 0 auto; background: var(--surface); border-bottom: 1px solid var(--border); display: none; }
  .nav[data-open="true"] { display: block; }
  .nav ul { flex-direction: column; padding: .75rem 1.25rem 1.25rem; }
}

.btn { display: inline-flex; align-items: center; justify-content: center; gap: .5rem; min-height: 48px;
  padding: .75rem 1.4rem; border-radius: 999px; border: 2px solid transparent; font-weight: 700;
  text-decoration: none; cursor: pointer; font: inherit; font-weight: 700; }
.btn--primary { background: var(--accent); color: var(--on-accent); }
.btn--ghost { border-color: currentColor; color: inherit; background: transparent; }

.hero { position: relative; isolation: isolate; display: grid; align-items: center;
  min-height: min(86svh, 760px); color: #fff; overflow: clip; }
.hero__art { position: absolute; inset: 0; z-index: -2; width: 100%; height: 100%; object-fit: cover; }
.hero::after { content: ""; position: absolute; inset: 0; z-index: -1;
  background: linear-gradient(180deg, rgb(8 20 18 / .55), rgb(8 20 18 / .72)); }
.hero h1 { font-size: var(--step-4); max-width: 18ch; }
.hero p { font-size: 1.2rem; max-width: 52ch; color: #f1f5f4; }
.hero .btn--primary { background: #fff; color: #0b3b36; }
.hero .actions { display: flex; flex-wrap: wrap; gap: .75rem; margin-top: 1.5rem; }

.grid { display: grid; gap: 1.25rem; grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr)); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow); overflow: hidden; display: flex; flex-direction: column; }
.card__body { padding: 1.25rem 1.25rem 1.5rem; display: grid; gap: .35rem; }
.card p { color: var(--muted); margin: 0; }
.price { font-size: 1.35rem; font-weight: 800; color: var(--text); }
.price small { font-size: .9rem; font-weight: 600; color: var(--muted); }
figure { margin: 0; }
figcaption { padding: .75rem 1rem; color: var(--muted); font-size: .95rem; }
blockquote { margin: 0; }

.form { display: grid; gap: 1rem; max-width: 40rem; }
.field { display: grid; gap: .35rem; }
.field label { font-weight: 600; }
.field input, .field textarea { font: inherit; color: var(--text); background: var(--surface);
  border: 1.5px solid var(--border); border-radius: .75rem; padding: .8rem 1rem; min-height: 48px; }
.field [aria-invalid="true"] { border-color: light-dark(#b91c1c, #fca5a5); }
.error { color: light-dark(#b91c1c, #fca5a5); font-size: .95rem; min-height: 1.4em; }
.status { font-weight: 600; color: var(--accent); }
.map { width: 100%; aspect-ratio: 16 / 9; border: 0; border-radius: var(--radius); }

.site-footer { padding-block: 2.5rem; border-top: 1px solid var(--border); color: var(--muted); }

@media (prefers-reduced-motion: no-preference) {
  @supports (animation-timeline: view()) {
    .reveal { animation: reveal linear both; animation-timeline: view(); animation-range: entry 0% cover 25%; }
  }
  .card { transition: transform .2s ease, box-shadow .2s ease; }
  .card:hover { transform: translateY(-4px); }
}
@keyframes reveal { from { opacity: 0; translate: 0 1.5rem; } to { opacity: 1; translate: 0 0; } }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
```

## JavaScript
```js
// В <head>, преди CSS: <script>try{const t=localStorage.getItem('theme');if(t)document.documentElement.dataset.theme=t}catch(e){}</script>
const root = document.documentElement;
const themeBtn = document.querySelector('[data-theme-toggle]');
const isDark = () => (root.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
const paintThemeBtn = () => { themeBtn?.setAttribute('aria-pressed', String(isDark())); if (themeBtn) themeBtn.textContent = isDark() ? '☀' : '☾'; };
themeBtn?.addEventListener('click', () => {
  root.dataset.theme = isDark() ? 'light' : 'dark';
  try { localStorage.setItem('theme', root.dataset.theme); } catch (e) {}
  paintThemeBtn();
});
paintThemeBtn();

const navBtn = document.querySelector('[data-nav-toggle]');
const nav = navBtn && document.getElementById(navBtn.getAttribute('aria-controls'));
navBtn?.addEventListener('click', () => {
  const open = navBtn.getAttribute('aria-expanded') === 'true';
  navBtn.setAttribute('aria-expanded', String(!open));
  nav.dataset.open = String(!open);
});
nav?.addEventListener('click', e => { if (e.target.closest('a')) { navBtn.setAttribute('aria-expanded', 'false'); nav.dataset.open = 'false'; } });

for (const form of document.querySelectorAll('form[data-validate]')) {
  form.addEventListener('submit', e => {
    e.preventDefault();
    let first = null;
    for (const field of form.querySelectorAll('input, textarea, select')) {
      const valid = field.checkValidity();
      field.setAttribute('aria-invalid', String(!valid));
      const err = document.getElementById(field.getAttribute('aria-describedby'));
      if (err) err.textContent = valid ? '' : field.validity.valueMissing ? 'Попълнете полето.'
        : field.type === 'email' ? 'Въведете валиден имейл, напр. ivan@abv.bg.' : 'Проверете стойността.';
      if (!valid && !first) first = field;
    }
    const status = form.querySelector('[role="status"]');
    if (first) { first.focus(); if (status) status.textContent = ''; return; }
    if (status) status.textContent = 'Благодарим! Ще се свържем с вас до 24 часа.';
    form.reset();
  });
}
document.querySelectorAll('[data-year]').forEach(el => { el.textContent = new Date().getFullYear(); });
```

## HTML скелет
```html
<!doctype html>
<html lang="bg">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Име — какво е, къде</title>
  <meta name="description" content="Едно изречение: какво, къде, за кого.">
  <meta property="og:title" content="Име — какво е, къде">
  <meta property="og:type" content="website">
  <script>try{const t=localStorage.getItem('theme');if(t)document.documentElement.dataset.theme=t}catch(e){}</script>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <a class="skip-link" href="#main">Към съдържанието</a>
  <header class="site-header">
    <div class="container">
      <a class="brand" href="#top">Име</a>
      <nav class="nav" id="nav" aria-label="Основна навигация">
        <ul><li><a href="#about">За нас</a></li><li><a href="#offer">Предлагаме</a></li><li><a href="#contact">Контакт</a></li></ul>
      </nav>
      <button class="icon-btn" type="button" data-theme-toggle aria-label="Смени светла/тъмна тема">☾</button>
      <button class="icon-btn nav-toggle" type="button" data-nav-toggle aria-controls="nav" aria-expanded="false" aria-label="Меню">☰</button>
    </div>
  </header>
  <main id="main">
    <section class="hero" id="top">
      <svg class="hero__art" viewBox="0 0 1440 800" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
        <defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f6c28b"/><stop offset="1" stop-color="#e9855b"/></linearGradient></defs>
        <rect width="1440" height="800" fill="url(#sky)"/>
        <path d="M0 520 L240 330 L420 470 L640 260 L900 500 L1120 340 L1440 540 V800 H0Z" fill="#2f5d50"/>
        <path d="M0 640 L300 480 L560 620 L820 470 L1100 640 L1440 520 V800 H0Z" fill="#1f4037"/>
      </svg>
      <div class="container">
        <h1>Заглавие, което казва ползата</h1>
        <p>Едно-две изречения конкретно съдържание.</p>
        <div class="actions"><a class="btn btn--primary" href="#contact">Основно действие</a><a class="btn btn--ghost" href="#offer">Вижте повече</a></div>
      </div>
    </section>
    <section class="section" id="about">
      <div class="container"><h2>За нас</h2><p class="lead">Кои сте, откога, с какво сте различни — конкретно.</p></div>
    </section>
    <section class="section section--alt" id="offer">
      <div class="container">
        <h2>Предлагаме</h2>
        <div class="grid">
          <article class="card reveal"><div class="card__body"><h3>Услуга</h3><p>Описание.</p><p class="price">45 € <small>/ час</small></p></div></article>
        </div>
      </div>
    </section>
    <section class="section" id="contact">
      <div class="container">
        <h2>Контакт</h2>
        <form class="form" data-validate novalidate>
          <div class="field"><label for="name">Име</label><input id="name" name="name" required autocomplete="name" aria-describedby="name-error"><span class="error" id="name-error"></span></div>
          <div class="field"><label for="email">Имейл</label><input id="email" name="email" type="email" required autocomplete="email" aria-describedby="email-error"><span class="error" id="email-error"></span></div>
          <button class="btn btn--primary" type="submit">Изпрати</button>
          <p class="status" role="status" aria-live="polite"></p>
        </form>
      </div>
    </section>
  </main>
  <footer class="site-footer"><div class="container">© <span data-year>2026</span> Име</div></footer>
  <script src="script.js" defer></script>
</body>
</html>
```

## Python Код
```python
"""Контраст по WCAG 2.2 — за проверка на цвят, различен от токените."""


def _channel(v: int) -> float:
    c = v / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """Съотношение на контраста (1…21). Текст ≥ 4.5, едър текст/бутони ≥ 3."""
    a, b = luminance(fg), luminance(bg)
    return round((max(a, b) + 0.05) / (min(a, b) + 0.05), 2)


if __name__ == "__main__":
    assert contrast_ratio("#000", "#fff") == 21.0
    assert contrast_ratio("#ffffff", "#0f766e") >= 4.5      # бутон, светла тема
    assert contrast_ratio("#042f2e", "#5eead4") >= 4.5      # бутон, тъмна тема
    assert contrast_ratio("#57534e", "#faf8f5") >= 4.5      # приглушен текст
    assert contrast_ratio("#a8a29e", "#12100e") >= 4.5
    assert contrast_ratio("#0f766e", "#f1ede6") >= 4.5      # връзка върху --surface-2
    assert contrast_ratio("#ffffff", "#a8e6cf") < 3         # грешката от 2026-09-28
    print("OK")
```
