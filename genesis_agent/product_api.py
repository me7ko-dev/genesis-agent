#!/usr/bin/env python3
"""
genesis_agent.product_api — продуктов слой (L2-5).

Прост FastAPI сервис + вграден web UI: потребител подава кодинг задача, Genesis
връща ВЕРИФИЦИРАН код (минал sandbox тест). Основа за реален продукт/монетизация.

/v1/chat/completions (2026-07-26): OpenAI-съвместим разговорен endpoint за
мобилни/IDE клиенти — истинската Brain логика
(RAG + адаптивно рутиране + облачен fallback), не sandbox-верифициран pipeline.

Безопасност:
  - Мозъкът работи от страната на сървъра; API ключовете НИКОГА не се пращат към клиента.
  - Генерираният код се изпълнява само през genesis_agent.sandbox (опасното се блокира).
  - Прост rate limit на IP.
  - Bound на 0.0.0.0 (достъпен от локалната мрежа) — само rate-limit, БЕЗ auth. Не
    излагай директно на публичния интернет без допълнителна автентикация.

Пускане:
    python3 -m genesis_agent.product_api          # http://0.0.0.0:8100
или:
    uvicorn genesis_agent.product_api:app --host 0.0.0.0 --port 8100
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from genesis_agent.brain import LOCAL_TIER_MAX, LOCAL_TIER_NORMAL, Brain, set_local_only
from genesis_agent.orchestrator import run_orchestrated

# Изричен офлайн режим през /v1/chat/completions (design note, 2026-07-31):
# клиентите тук (genesis-web-agent) нямат достъп до Python import-и, само до
# JSON — затова моделният избор минава през самото OpenAI "model" поле вместо
# нов custom параметър. "local-max"/"local-normal" са sentinel стойности,
# разпознати само тук; всяка друга (или липсваща) стойност си остава обичайният
# облак-пръв ред. Реалните имена на моделите са в genesis_agent.brain, за да не
# се разминат с терминала/GUI-то.
_LOCAL_MODE_ALIASES = {"local-max": LOCAL_TIER_MAX, "local-normal": LOCAL_TIER_NORMAL}

app = FastAPI(title="Genesis — Verified Code Service")

_RATE: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 10          # заявки
_RATE_WINDOW = 60         # за 60 секунди
_RATE_LOCK = threading.Lock()

# Сериализира глобалната мутация на режима в /v1/chat/completions — виж
# бележката в самия endpoint защо изобщо е нужен.
_LOCAL_MODE_LOCK = threading.Lock()


class SolveRequest(BaseModel):
    goal: str


class ChatMsg(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMsg]
    temperature: float | None = None


def _check_rate(ip: str) -> None:
    now = time.time()
    # Под lock: FastAPI обслужва sync endpoint-ите в threadpool, значи _RATE се
    # чете и пише от няколко нишки едновременно.
    with _RATE_LOCK:
        # Изхвърляме и записите на ДРУГИ, замлъкнали IP-та, не само подрязваме
        # текущия (bug fix, 2026-08-12): преди се пипаше само списъкът на
        # подателя, а ключът на всеки виждан някога IP оставаше в речника
        # завинаги. При услуга, вързана на 0.0.0.0, това е неограничен растеж,
        # ключуван по стойност, която изпращачът контролира.
        for k in [k for k, hits in _RATE.items()
                  if k != ip and (not hits or now - hits[-1] >= _RATE_WINDOW)]:
            del _RATE[k]

        _RATE[ip] = [t for t in _RATE[ip] if now - t < _RATE_WINDOW]
        if len(_RATE[ip]) >= _RATE_LIMIT:
            raise HTTPException(status_code=429,
                                detail="Твърде много заявки. Опитай пак след минута.")
        _RATE[ip].append(now)


@app.post("/solve")
def solve(req: SolveRequest, request: Request):
    _check_rate(request.client.host if request.client else "unknown")
    goal = (req.goal or "").strip()
    if not goal or len(goal) < 5:
        raise HTTPException(status_code=400, detail="Задай смислена задача (мин. 5 символа).")

    out = run_orchestrated(goal + " Include an assert self-test that prints OK.", max_rounds=4)
    code = ""
    if out.success and out.skill_path:
        try:
            from pathlib import Path

            from genesis_agent.skill_loader import skill_view
            code = skill_view(Path(out.skill_path).stem)["code"]
        except Exception:
            code = ""
    return JSONResponse({
        "success": out.success,
        "verified": out.success,   # успех = мина sandbox тест
        "rounds": out.rounds,
        "plan": out.plan[:600],
        "code": code,
        "error": out.last_error[:300] if not out.success else "",
    })


_IDE_CHAT_SYSTEM_PROMPT = (
    "Ти си Genesis Agent — опитен software engineer, работещ на машината на потребителя. "
    "Помагаш за писане, обяснение, дебъгване и разширяване на код и ЦЕЛИ приложения — на всякакъв "
    "език (Swift, Python, JavaScript, HTML/CSS, shell и др.), не само единични Python функции с "
    "self-test. Можеш да обясняваш идеи, да предлагаш архитектура, да пишеш многофайлови приложения "
    "парче по парче, и да отговаряш разговорно, когато въпросът не изисква код. Когато пишеш код — "
    "връщай го в подходящ markdown code fence (```swift, ```python, ```html и т.н.), с кратко "
    "обяснение преди или след ако е полезно. Работиш с пълните възможности на локалния Genesis "
    "мозък — същите като на лаптопа, не орязана мобилна версия.\n\n"
    "Когато промяна засяга ПОВЕЧЕ ОТ ЕДИН файл (напр. 'направи ми login екран' → нови файлове + "
    "промяна в съществуващ), маркирай ВСЕКИ файл отделно с ред във формат:\n"
    "### FILE: относителен/път/до/Файл.swift\n"
    "веднага последван от нормален markdown code fence с ПЪЛНОТО съдържание на файла (не diff, не "
    "откъс — целия файл готов за записване, дори ако само малка част се променя). Пример:\n\n"
    "### FILE: Views/Login/LoginView.swift\n"
    "```swift\n"
    "import SwiftUI\n"
    "// ... пълен код на файла ...\n"
    "```\n\n"
    "### FILE: Views/Login/LoginViewModel.swift\n"
    "```swift\n"
    "// ... пълен код ...\n"
    "```\n\n"
    "Правила:\n"
    "- Всеки файл има собствен '### FILE: път' ред точно преди своя code fence — нищо друго на "
    "този ред.\n"
    "- Пътят е относителен спрямо корена на проекта, с '/' между папки (липсващите папки ще бъдат "
    "създадени автоматично) — без водещо '/' и без '..'.\n"
    "- Винаги давай ПЪЛНОТО съдържание на файла, дори при редакция на съществуващ файл — "
    "приложението заменя файла изцяло, не merge-va откъси.\n"
    "- Свободен текст между или около '### FILE:' блоковете е ОК, той се игнорира от парсъра.\n"
    "- Ако задачата не изисква промяна на файлове в проекта — НЕ използвай '### FILE:' изобщо, "
    "просто отговори нормално.\n"
    "- За единични 'поправи този бъг в текущия файл' отговори е достатъчен обикновен code fence "
    "без '### FILE:' маркер."
)


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest, request: Request):
    """
    OpenAI-съвместим endpoint (за произволен OpenAI-съвместим клиент).
    За разлика от /solve (тежък, sandbox-верифициран pipeline), тук е разговорна
    Brain логика: адаптивно рутиране (route_for_goal) между локалните tier-ове
    (3b/7b/14b) + автоматичен облачен fallback ако локалният модел не е наличен.
    БЕЗ build_context() RAG инжекция — тя е направена за автономния мисиен цикъл
    (изрично казва "преизползвай/разшири този код") и разбива разговорния тон тук:
    засечено наживо — семантично несвързан "skill" (disk usage скрипт) се
    инжектираше дори за просто "как си", принуждавайки модела towards код.
    """
    _check_rate(request.client.host if request.client else "unknown")
    user_messages = [m for m in req.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="Нужно е поне едно 'user' съобщение.")
    goal = user_messages[-1].content

    local_model = _LOCAL_MODE_ALIASES.get((req.model or "").strip().lower())

    messages = [{"role": "system", "content": _IDE_CHAT_SYSTEM_PROMPT}]
    messages += [{"role": m.role, "content": m.content} for m in req.messages]

    # Под lock, обхващащ set_local_only → Brain() → complete() (bug fix,
    # 2026-08-12). `set_local_only` мутира състояние за ЦЕЛИЯ процес —
    # os.environ["GENESIS_LOCAL_ONLY"] плюс модулната brain.LOCAL_MODEL — а
    # Brain.complete() чете env променливата в момента на извикването си, не
    # при конструиране. FastAPI обслужва sync endpoint-и (`def`, не `async
    # def`) в threadpool, така че две едновременни заявки се тъпчеха взаимно:
    # клиент, поискал "local-max", тихо получаваше облачен отговор, защото
    # друг клиент е извикал set_local_only(None) между двата реда; или обратно
    # — заявка без модел биваше прехвърлена в локален режим от чужд избор.
    # Сериализирането прави chat completions последователни, което за тази
    # услуга (личен мобилен/IDE клиент, документирана като без auth) е
    # правилната размяна: и без това всяка заявка чака LLM няколко секунди, а
    # алтернативата е да изпълняваме избора на ГРЕШНИЯ клиент. Истинската
    # поправка е per-request local-only параметър в Brain вместо глобално
    # състояние — по-голяма промяна в brain.py, отделна задача.
    with _LOCAL_MODE_LOCK:
        set_local_only(local_model)
        try:
            brain = Brain()
            if not local_model:
                brain.route_for_goal(goal)
            reply = brain.complete(messages)
        finally:
            # Режимът е за ТАЗИ заявка — не бива да остане включен за
            # следващата, която не го е искала.
            set_local_only(None)
    content = reply.raw_text or "(празен отговор от Genesis)"

    return JSONResponse({
        "id": "genesis-chatcmpl",
        "object": "chat.completion",
        "model": (brain.current or {}).get("model", "genesis-agent"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    })


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _UI


@app.get("/health")
def health():
    return {"status": "online", "service": "genesis-verified-code"}


_UI = """<!doctype html>
<html lang="bg"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Genesis — Verified Code</title>
<style>
 :root { color-scheme: light dark; }
 body { font-family: system-ui, sans-serif; max-width: 780px; margin: 2rem auto; padding: 0 1rem;
        background: Canvas; color: CanvasText; }
 h1 { font-size: 1.5rem; } .sub { opacity:.7; margin-top:-.5rem; }
 textarea { width:100%; min-height:90px; font-size:1rem; padding:.6rem; border-radius:8px;
            border:1px solid #8888; background:Field; color:FieldText; box-sizing:border-box; }
 button { margin-top:.6rem; padding:.6rem 1.2rem; font-size:1rem; border:0; border-radius:8px;
          background:#4f46e5; color:#fff; cursor:pointer; }
 button:disabled { opacity:.5; cursor:wait; }
 pre { background:#1113; padding:1rem; border-radius:8px; overflow-x:auto; white-space:pre-wrap; }
 .ok { color:#16a34a; font-weight:bold; } .err { color:#dc2626; font-weight:bold; }
 .box { margin-top:1rem; }
</style></head><body>
 <h1>🧠 Genesis — Verified Code</h1>
 <p class="sub">Опиши задача. Genesis пише код, тества го в sandbox и връща само работещото.</p>
 <textarea id="goal" placeholder="напр. функция, която проверява дали число е просто"></textarea><br>
 <button id="go" onclick="solve()">Реши</button>
 <div class="box" id="out"></div>
<script>
async function solve(){
 const g=document.getElementById('goal').value.trim();
 const out=document.getElementById('out'), btn=document.getElementById('go');
 if(g.length<5){out.innerHTML='<span class="err">Задай по-конкретна задача.</span>';return;}
 btn.disabled=true; out.innerHTML='⏳ Genesis мисли и тества...';
 try{
  const r=await fetch('/solve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({goal:g})});
  const d=await r.json();
  if(!r.ok){out.innerHTML='<span class="err">'+(d.detail||'грешка')+'</span>';}
  else if(d.success){out.innerHTML='<p class="ok">✅ Verified (мина sandbox тест) — '+d.rounds+' рунда</p>'+
    '<pre>'+escapeHtml(d.code)+'</pre>';}
  else{out.innerHTML='<p class="err">❌ Неуспех: '+escapeHtml(d.error||'')+'</p>';}
 }catch(e){out.innerHTML='<span class="err">Мрежова грешка.</span>';}
 btn.disabled=false;
}
function escapeHtml(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    print("Genesis Verified Code Service → http://0.0.0.0:8100 (/solve, /v1/chat/completions)")
    uvicorn.run(app, host="0.0.0.0", port=8100, log_level="warning")
