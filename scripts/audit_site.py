#!/usr/bin/env python3
"""Audit a served static site in headless Chromium: console errors, failed requests,
mobile overflow, text contrast (WCAG), theme toggle, a11y basics, screenshots.

    python scripts/audit_site.py http://127.0.0.1:8000/ out_dir

Measured the website trials in NEXT_STEPS (plan G.11, 2026-09-28). Needs playwright
(the system Python on the laptop has it; the pipx venv does not)."""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

url, out = sys.argv[1], Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
CHECKS = r"""
() => {
  const lum = c => { const m = c.match(/[\d.]+/g); if (!m) return null;
    const [r,g,b] = m.slice(0,3).map(v => { v/=255; return v<=0.03928? v/12.92 : ((v+0.055)/1.055)**2.4; });
    return 0.2126*r+0.7152*g+0.0722*b; };
  const bgOf = el => { for (let e=el; e; e=e.parentElement) { const s=getComputedStyle(e);
      if (s.backgroundImage !== 'none') return {grad: s.backgroundImage};
      const m=s.backgroundColor.match(/[\d.]+/g); if (m && (m.length<4 || +m[3]>0.5)) return {color: s.backgroundColor}; }
    return {color: 'rgb(255,255,255)'}; };
  const low = [];
  for (const el of document.querySelectorAll('h1,h2,h3,p,a,li,label,button,blockquote,span')) {
    if (!el.offsetParent && getComputedStyle(el).position!=='fixed') continue;
    const txt = [...el.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent.trim()).join(''); if (!txt) continue;
    const bg = bgOf(el); if (bg.grad) { low.push({el: el.tagName, text: txt.slice(0,40), note: 'текст върху градиент: '+bg.grad.slice(0,80)}); continue; }
    const L1 = lum(getComputedStyle(el).color), L2 = lum(bg.color); if (L1==null||L2==null) continue;
    const ratio = (Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const big = parseFloat(getComputedStyle(el).fontSize) >= 24;
    if (ratio < (big?3:4.5)) low.push({el: el.tagName, text: txt.slice(0,40), ratio: +ratio.toFixed(2)});
  }
  return {
    title: document.title,
    overflowX: document.documentElement.scrollWidth > window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    imgsNoAlt: [...document.images].filter(i => !i.hasAttribute('alt')).length,
    images: document.images.length,
    svgNoRoleOrHidden: [...document.querySelectorAll('svg')].filter(s => !s.getAttribute('aria-hidden') && !s.getAttribute('role') && !s.querySelector('title')).length,
    brokenAnchors: [...document.querySelectorAll('a[href^="#"]')].map(a=>a.getAttribute('href')).filter(h => h.length>1 && !document.querySelector(h)),
    inputsNoLabel: [...document.querySelectorAll('input,textarea,select')].filter(i => !(i.labels && i.labels.length) && !i.getAttribute('aria-label')).length,
    h1: document.querySelectorAll('h1').length,
    themeToggle: !!document.querySelector('[aria-label*="тем" i],[data-theme-toggle],#theme-toggle,.theme-toggle'),
    lowContrast: low.slice(0, 15), lowContrastCount: low.length,
    fonts: [...new Set([...document.querySelectorAll('body,h1,h2,p')].map(e => getComputedStyle(e).fontFamily))],
    htmlBytes: document.documentElement.outerHTML.length,
  };
}
"""
report = {}
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, vp, scheme in [("desktop-light", (1440, 900), "light"), ("desktop-dark", (1440, 900), "dark"), ("mobile-light", (390, 844), "light")]:
        ctx = b.new_context(viewport={"width": vp[0], "height": vp[1]}, color_scheme=scheme, device_scale_factor=1,
                            is_mobile=name.startswith("mobile"), has_touch=name.startswith("mobile"))
        page = ctx.new_page(); errs, failed = [], []
        page.on("console", lambda m, errs=errs: errs.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e, errs=errs: errs.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r, failed=failed: failed.append(f"{r.url} ({r.failure})"))
        page.on("response", lambda r, failed=failed: failed.append(f"{r.url} HTTP {r.status}") if r.status >= 400 else None)
        page.goto(url, wait_until="networkidle")
        # scroll through so reveal animations fire
        h = page.evaluate("document.documentElement.scrollHeight")
        for y in range(0, h + vp[1], vp[1] // 2):
            page.evaluate(f"window.scrollTo(0,{y})"); page.wait_for_timeout(120)
        page.evaluate("window.scrollTo(0,0)"); page.wait_for_timeout(800)
        r = page.evaluate(CHECKS); r["consoleErrors"] = errs; r["failedRequests"] = failed
        page.screenshot(path=str(out / f"{name}-top.png"))
        page.screenshot(path=str(out / f"{name}-full.png"), full_page=True)
        report[name] = r; ctx.close()
    b.close()
(out / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
print(json.dumps(report, ensure_ascii=False, indent=1))
