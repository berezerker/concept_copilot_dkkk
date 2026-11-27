"""FastAPI-сервер: загрузка модели, потоковый диалог с агентом (SSE), выгрузка результатов."""
from __future__ import annotations

import io
import json
import os
import queue
import tempfile
import threading
import time

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel, Field

from . import config
from .agent.harness import run_turn
from .agent.session import Session
from .llm import available_providers, make_client, LLMError

app = FastAPI(title="Копилот ДККК · агент CashFlow")
STATIC = os.path.join(config.ROOT, "static")
SESSIONS: dict[str, Session] = {}
APP_VERSION = "2026-11-14.1"
if config.env_bool("CF_RECORD"):                     # запись сеанса для интерактивного демо (tools/build_demo.py)
    from .recorder import RecorderMiddleware
    app.add_middleware(RecorderMiddleware)
MAX_UPLOAD_MB = int(config.env("CF_MAX_UPLOAD_MB", "100"))


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-store"})


@app.get("/api/version")
def version():
    return {"version": APP_VERSION}


@app.get("/api/config")
def get_config():
    return {"providers": available_providers(), "workdir": config.workdir(),
            "default_provider": config.env("CF_DEFAULT_PROVIDER", "mock"),
            "engine": config.env("CF_ENGINE", "auto")}


@app.post("/api/upload")
def upload(file: UploadFile = File(...), session_id: str | None = None):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Нужен файл .xlsx или .xlsm")
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
        size = 0
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * (1 << 20):
                raise HTTPException(413, f"Файл больше {MAX_UPLOAD_MB} МБ")
            tmp.write(chunk)
        path = tmp.name
    return _load_into_session(path, file.filename, session_id)


def _load_into_session(path, filename, session_id=None, keep=False):
    s = SESSIONS.get(session_id) if session_id else None
    if s is None:
        s = Session(config.workdir())
        SESSIONS[s.id] = s
    t0 = time.time()
    from .logbus import BUS
    oid = BUS.begin(f"Загрузка модели «{filename}»", s.id)
    try:
        with s.lock:
            card = s.load(path, filename, config.cache_dir(), config.env("CF_ENGINE"),
                          progress=lambda m: (BUS.log(m, "progress", sid=s.id), BUS.step(oid, m)))
        BUS.end(oid, True, f"{card['sheets_total']} листов, {card['formulas_total']} формул, движок: "
                           f"{s.engine.name if s.engine_open else 'нет — ' + str(s.engine_error)[:120]}")
    except Exception as e:
        BUS.end(oid, False, str(e)[:200])
        raise HTTPException(400, f"Не удалось прочитать модель: {type(e).__name__}: {e}")
    finally:
        if not keep:
            os.unlink(path)
    from .workbook import semantics as sm, manifest as mf
    return {"session_id": s.id, "card": card, "seconds": round(time.time() - t0, 1), "semantics": sm.summary(s.sem),
            "manifest": mf.summary(s.manifest),
            "engine": s.engine.name if s.engine_open else None, "engine_error": s.engine_error}


class PathIn(BaseModel):
    path: str
    session_id: str | None = None


@app.post("/api/upload_path")
def upload_path(req: PathIn):
    """Загрузить модель по пути на этом компьютере (для запуска из скриптов и ссылок вида /?model=путь).
    Только .xlsx/.xlsm внутри домашней папки пользователя; сервер слушает только 127.0.0.1."""
    p = os.path.realpath(os.path.expanduser(req.path))
    if not p.startswith(os.path.realpath(os.path.expanduser("~"))) or not p.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Нужен путь к .xlsx/.xlsm внутри домашней папки")
    if not os.path.exists(p):
        raise HTTPException(404, f"Файл не найден: {p}")
    s = SESSIONS.get(req.session_id) if req.session_id else None
    if s is None:
        s = Session(config.workdir())
        SESSIONS[s.id] = s
    from .logbus import BUS
    t0 = time.time()
    oid = BUS.begin(f"Загрузка модели «{os.path.basename(p)}»", s.id)
    try:
        card = s.load(p, os.path.basename(p), config.cache_dir(), config.env("CF_ENGINE"),
                      progress=lambda m: (BUS.log(m, "progress", sid=s.id), BUS.step(oid, m)))
        BUS.end(oid, True, f"движок: {s.engine.name if s.engine_open else 'нет — ' + str(s.engine_error)[:120]}")
    except Exception as e:
        BUS.end(oid, False, str(e)[:200])
        raise HTTPException(400, f"Не удалось прочитать модель: {type(e).__name__}: {e}")
    from .workbook import semantics as sm, manifest as mf
    return {"session_id": s.id, "card": card, "seconds": round(time.time() - t0, 1), "semantics": sm.summary(s.sem),
            "manifest": mf.summary(s.manifest),
            "engine": s.engine.name if s.engine_open else None, "engine_error": s.engine_error}


class ChatIn(BaseModel):
    session_id: str
    message: str
    provider: str = "mock"
    model: str | None = None
    plan_mode: bool = True


@app.post("/api/chat")
def chat(req: ChatIn):
    s = _session(req.session_id)
    client = _client(req.provider, req.model, s.id)
    def job(emit):
        from .logbus import wait_lock
        with wait_lock(s.lock, emit, s.id):
            _ensure_sem(s, client, emit)
        run_turn(s, client, req.message, emit, plan_mode=req.plan_mode)
    return _sse(job, "Агент: " + req.message[:60], s.id)


LOGGED = {"tool_start", "tool_end", "progress", "status", "error"}


def _sse(job, name="задача", sid=None):
    """Запустить job(emit) в потоке и отдавать события браузеру по SSE; всё важное — в журнал расчётов."""
    from .logbus import BUS, Cancelled
    q: queue.Queue = queue.Queue()
    state = {}

    def emit(kind, data):
        q.put((kind, data))
        if kind in LOGGED:
            oid = state.get("oid")
            if kind == "tool_start":
                txt = f"→ {data.get('name')} {json.dumps(data.get('args'), ensure_ascii=False, default=str)[:220]}"
                BUS.step(oid, data.get("name"))
            elif kind == "tool_end":
                txt = f"{'✓' if data.get('ok') else '✗'} {data.get('name')} — {data.get('seconds')} с"
                if not data.get("ok"):
                    txt += f": {str((data.get('result') or {}).get('error', ''))[:200]}"
            else:
                txt = data.get("text", "")
                if kind == "progress":
                    BUS.step(oid, txt)
            BUS.log(txt, "tool" if kind.startswith("tool") else kind, "error" if kind == "error" or
                    (kind == "tool_end" and not data.get("ok")) else "info", sid)

    def worker():
        if sid:
            BUS.clear_cancel(sid)
        state["oid"] = BUS.begin(name, sid)
        ok, note = True, ""
        try:
            job(emit)
        except Cancelled as e:
            ok, note = False, "остановлено"
            emit("error", {"text": f"⏹ {e}"})
        except Exception as e:  # noqa
            ok, note = False, f"{type(e).__name__}"
            emit("error", {"text": f"{type(e).__name__}: {e}"})
        finally:
            BUS.end(state["oid"], ok, note)
            if sid:
                BUS.clear_cancel(sid)
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                yield "event: done\ndata: {}\n\n"
                return
            kind, data = item
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/logs")
def logs(after: int = 0):
    """Журнал расчётов: поток событий + пульс (что сейчас считается и сколько секунд без новых событий)."""
    from .logbus import BUS

    def stream():
        seq = after
        while True:
            items = BUS.since(seq, timeout=2.0)
            for x in items:
                seq = max(seq, x["seq"])
                yield f"event: log\ndata: {json.dumps(x, ensure_ascii=False)}\n\n"
            yield f"event: pulse\ndata: {json.dumps({'ops': BUS.status(), 'seq': seq}, ensure_ascii=False)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/cancel/{session_id}")
def cancel(session_id: str):
    from .logbus import BUS
    BUS.request_cancel(session_id)
    return {"ok": True}


class DirectIn(BaseModel):
    session_id: str
    kind: str
    force: bool = False
    query: str | None = None
    provider: str = "mock"
    model: str | None = None


DIRECT = {"map": ("model_map", "Карта модели"), "audit": ("audit_model", "Предпроверка"),
          "deps": ("dependency_analysis", "Анализ зависимостей по факторам"),
          "params": ("find_parameters", "Реестр параметров модели")}

DEPS_SYSTEM = """Ты — старший кредитный аналитик ДККК. Тебе дают результаты автоматического анализа финансовой модели
заёмщика: доли экономических факторов в чувствительности ключевых метрик, выводы и сцепки факторов (что через что
пересчитывается в модели). Напиши для коллеги-кредитчика короткий разбор в markdown, по-русски:
## Вывод — 2–4 предложения: от чего на самом деле зависит устойчивость заёмщика и насколько риск сконцентрирован.
## Как устроена модель — 3–5 пунктов: ключевые связи простыми словами («внутренние цены нефтепродуктов считаются
через курс», «выручка сегментов просто индексируется на инфляцию»), что из этого нормально, а что стоит проверить.
## Что проверить и спросить у клиента — 3–5 пунктов.
Только цифры и названия из данных. Без адресов ячеек в тексте. Не повторяй таблицы.
Факты и гипотезы раздельно: утверждай только то, что посчитали инструменты (цифры, доли, пробои). Выводы сверх расчёта помечай «гипотеза:» / «стоит проверить:». Если чувствительность метрики ко всем факторам равна 0 или инструмент вернул ошибку — прямо так и напиши, не подменяй расчёт экспертными рассуждениями. Сцепки факторов — это связи формул, а не доказательство цикла или концентрации риска."""


@app.post("/api/direct")
def direct_run(req: DirectIn):
    """Кнопки (карта, предпроверка, факторы) — потоком, с журналом и остановкой. «Факторы» с выбранной LLM:
    сначала LLM-описания строк (если ещё нет), затем расчёт по факторам и разбор простыми словами."""
    s = _session(req.session_id)
    if req.kind not in DIRECT:
        raise HTTPException(400, "Неизвестная функция")
    from .agent.registry import TOOL_MAP
    from .logbus import wait_lock
    tool, title = DIRECT[req.kind]
    client = _client(req.provider, req.model, s.id) if req.kind == "deps" else None

    def job(emit):
        import time as _t
        with wait_lock(s.lock, emit, s.id):
            if client is not None:
                _ensure_sem(s, client, emit)
            t0 = _t.time()
            args = {"force": True} if req.force else {}
            if req.kind == "params":
                args = {"query": req.query or "", "limit": 400}
            emit("tool_start", {"id": "d1", "name": tool, "args": args})
            res = TOOL_MAP[tool]["fn"](s, args, emit)
            emit("tool_end", {"id": "d1", "name": tool, "ok": True, "seconds": round(_t.time() - t0, 1), "result": res})
        if client is not None and client.name != "mock":
            facts = {k: res.get(k) for k in ("tornados", "findings", "couplings", "industry")}
            emit("status", {"text": f"{client.describe()} пишет разбор…"})
            r = client.chat([{"role": "system", "content": DEPS_SYSTEM},
                             {"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)}], [], "auto")
            if (r.content or "").strip():
                emit("answer", {"text": r.content.strip()})
    return _sse(job, title, s.id)


def _ensure_sem(s, client, emit, force=False):
    """Смысл модели делает LLM по каждой модели, один раз (кэш по хэшу файла):
    1) справочник — сопоставление показателей, которые не нашлись по названию (catalog.llm_map);
    2) паспорт строк — деловые названия, описания, факторы (semantics.enrich).
    Справочник идёт первым: от найденных показателей зависит, какие строки попадут в паспорт."""
    from .workbook import catalog as cg, semantics as sm
    if client.name == "mock":
        return False
    done = False
    if s.catalog is not None and (force or not s.catalog.get("llm")):
        before = {k: (v["primary"]["sheet"], v["primary"]["row"]) for k, v in s.catalog["concepts"].items()}
        cg.llm_map(s.idx, s.catalog, client, s.forecast_start, emit)
        cg.save(s.cache_dir, s.catalog)
        after = {k: (v["primary"]["sheet"], v["primary"]["row"]) for k, v in s.catalog["concepts"].items()}
        if after != before:
            s.refresh_card()
            if not (s.sem and s.sem.get("llm")) or force:
                s.sem = sm.build_semantics(s.idx, s.card, s.forecast_start)
            emit("card", s.card)
        done = True
    if force or not (s.sem and s.sem.get("llm")):
        emit("status", {"text": f"Разбираю смысл модели ({client.describe()})…"})
        if force:
            s.sem = sm.build_semantics(s.idx, s.card, s.forecast_start)
        sm.enrich(s.sem, client, emit)
        from .logbus import BUS
        BUS.check(s.id)
        sm.refresh_formula_words(s.idx, s.sem, s.forecast_start)
        sm.save(s.cache_dir, s.sem)
        emit("semantics", sm.summary(s.sem))
        done = True
    if done:
        from .workbook import manifest as mf
        s.refresh_manifest()
        emit("manifest", mf.summary(s.manifest))
    return done


def _session(session_id):
    s = SESSIONS.get(session_id)
    if s is None or s.idx is None:
        raise HTTPException(400, "Сначала загрузите модель")
    return s


def _client(provider, model, sid=None):
    from .logbus import ClientProxy
    try:
        return ClientProxy(make_client(provider, model or None), sid)
    except LLMError as e:
        raise HTTPException(400, str(e))


class KpkiIn(BaseModel):
    session_id: str
    provider: str = "mock"
    model: str | None = None
    rules: list[dict]
    mc_runs: int = 80
    max_candidates: int = 40


@app.post("/api/kpki")
def kpki(req: KpkiIn):
    """Фиксированный сценарий «Заключение для КПКИ» (одинаковые шаги для каждой модели)."""
    s = _session(req.session_id)
    client = _client(req.provider, req.model, s.id)
    from .agent.kpki import run_kpki

    def job(emit):
        from .logbus import wait_lock
        with wait_lock(s.lock, emit, s.id):
            _ensure_sem(s, client, emit)
            run_kpki(s, client, req.model_dump(), emit)
    return _sse(job, "Заключение для КПКИ", s.id)


class ExpertIn(BaseModel):
    session_id: str
    provider: str = "mock"
    model: str | None = None
    news: str
    industry: str | None = None
    fields: list[dict]
    metrics: list[dict] = []
    rules: list[dict] = []


@app.post("/api/expert")
def expert(req: ExpertIn):
    """Отраслевой эксперт: новость + поля → новые допущения → стресс-тест модели."""
    s = _session(req.session_id)
    client = _client(req.provider, req.model, s.id)
    from .agent.registry import TOOL_MAP

    def job(emit):
        s.client = client
        from .logbus import wait_lock
        with wait_lock(s.lock, emit, s.id):
            _ensure_sem(s, client, emit)
        args = {"news": req.news, "industry": req.industry, "fields": req.fields, "metrics": req.metrics, "rules": req.rules}
        emit("tool_start", {"id": "exp", "name": "industry_expert", "args": args})
        import time as _t
        t0 = _t.time()
        with wait_lock(s.lock, emit, s.id):
            try:
                res, ok = TOOL_MAP["industry_expert"]["fn"](s, args, emit), True
            except Exception as e:
                res, ok = {"error": f"{type(e).__name__}: {e}"}, False
        emit("tool_end", {"id": "exp", "name": "industry_expert", "ok": ok, "seconds": round(_t.time() - t0, 1), "result": res})
    return _sse(job, "Отраслевой эксперт", s.id)


@app.get("/api/search/{session_id}")
def search(session_id: str, q: str, only_inputs: bool = False, limit: int = 12):
    """Поиск строк для выбора полей в окнах эксперта и КПКИ (без LLM)."""
    s = _session(session_id)
    from .agent.registry import TOOL_MAP
    return TOOL_MAP["search_rows"]["fn"](s, {"query": q, "only_inputs": only_inputs, "limit": limit}, lambda k, d: None)


DEFAULT_RULES = [("Чистый долг / EBITDA", "<=", 3.5), ("DSCR", ">=", 1.2), ("ISCR / ICR", ">=", 1.5)]


@app.get("/api/defaults/{session_id}")
def defaults(session_id: str):
    """Предложения для окон: ковенанты (метрики из карточки с типовыми порогами) и поля-драйверы."""
    s = _session(session_id)
    km = {m["metric"]: m["candidates"] for m in s.card["key_metrics"]}
    rules = []
    for name, op, thr in DEFAULT_RULES:
        for c in km.get(name, [])[:1]:
            rules.append({"sheet": c["sheet"], "row": c["row"], "name": name, "op": op, "threshold": thr, "label": c["label"]})
    metrics = [{"sheet": c["sheet"], "row": c["row"], "name": m, "label": c["label"]} for m, cs in list(km.items())[:12] for c in cs[:1]]
    from .workbook import semantics as sm
    fields = [{"sheet": d["sheet"], "row": d["row"], "name": sm.name(s, d["sheet"], d["row"])[:60], "unit": d["unit"]}
              for d in s.card["drivers"][:15]]
    return {"rules": rules, "metrics": metrics, "fields": fields}


class SemIn(BaseModel):
    session_id: str
    provider: str = "mock"
    model: str | None = None
    force: bool = False


@app.post("/api/semantics")
def semantics(req: SemIn):
    """Смысловой разбор модели (по умолчанию после загрузки): паспорт строк, факторы, описания от LLM, кэш."""
    s = _session(req.session_id)
    client = _client(req.provider, req.model, s.id)
    from .workbook import semantics as sm

    def job(emit):
        if client.name == "mock":
            emit("semantics", dict(sm.summary(s.sem), offline=True))
            return
        if s.sem and s.sem.get("llm") and (s.catalog or {}).get("llm") and not req.force:
            emit("semantics", dict(sm.summary(s.sem), cached=True))
            return
        emit("progress", {"text": f"Разбор смысла модели: справочник показателей и {len(s.sem['rows'])} строк, {client.describe()}…"})
        with s.lock:
            _ensure_sem(s, client, emit, force=req.force)
        emit("semantics", sm.summary(s.sem))
    return _sse(job, "Разбор смысла модели", s.id)


@app.get("/api/semantics/{session_id}")
def semantics_get(session_id: str):
    s = _session(session_id)
    from .workbook import semantics as sm
    rows = sorted(s.sem["rows"].values(), key=lambda p: (p["factor"], -p["fanout"]))
    return {"summary": sm.summary(s.sem),
            "rows": [{k: p.get(k) for k in ("sheet", "row", "name", "desc", "factor", "sub", "unit", "kind", "direction")}
                     for p in rows]}


class DocxIn(BaseModel):
    markdown: str
    filename: str = "Заключение_КПКИ.docx"


@app.post("/api/docx")
def to_docx(req: DocxIn):
    from urllib.parse import quote
    from .report_docx import md_to_docx
    return Response(md_to_docx(req.markdown),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(req.filename)}"})


def _direct(session_id, name):
    s = SESSIONS.get(session_id)
    if s is None or s.idx is None:
        raise HTTPException(400, "Сначала загрузите модель")
    from .agent.registry import TOOL_MAP
    events = []
    with s.lock:
        res = TOOL_MAP[name]["fn"](s, {}, lambda k, d: events.append((k, d)))
    return {"events": [{"type": k, "data": d} for k, d in events], "result": res}


@app.get("/api/map/{session_id}")
def model_map(session_id: str):
    """Карта модели без LLM (кнопка в интерфейсе)."""
    return _direct(session_id, "model_map")


@app.get("/api/manifest/{session_id}")
def model_manifest(session_id: str):
    """Единый паспорт модели для UI и внешних интеграций."""
    from .workbook import manifest as mf
    s = _session(session_id)
    with s.lock:
        return s.refresh_manifest() or mf.build(s)


class BindingIn(BaseModel):
    concept: str
    sheet: str
    row: int
    file_hash: str


@app.post("/api/manifest/{session_id}/binding")
def confirm_binding(session_id: str, req: BindingIn):
    from .agent.tools import t_catalog_set
    from .workbook.manifest import summary
    s = _session(session_id)
    with s.lock:
        if s.idx.file_hash != req.file_hash:
            raise HTTPException(409, "Книга изменилась. Откройте список привязок заново.")
        if req.concept not in s.catalog["concepts"]:
            raise HTTPException(400, "Показатель не найден в паспорте")
        try:
            result = t_catalog_set(s, req.model_dump(), lambda *_: None)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"binding": result, "manifest": summary(s.manifest), "card": s.card}


class CellStressIn(BaseModel):
    target: str | None = None
    metric: str | None = None
    period: str | None = None
    source_period: str | None = None
    query: str | None = None
    source: str | None = None
    pct: float | None = None
    file_hash: str | None = None
    expected_input: float | None = None
    run: bool = False


@app.post("/api/cell-stress/{session_id}")
def cell_stress(session_id: str, req: CellStressIn):
    from .agent.tools_probe import inspect_cell_stress, run_cell_stress
    from .workbook.transaction import RestorationError
    s = _session(session_id)
    with s.lock:
        if req.file_hash and req.file_hash != s.idx.file_hash:
            raise HTTPException(409, "Книга изменилась. Откройте сценарий заново.")
        fn = run_cell_stress if req.run else inspect_cell_stress
        try:
            return fn(s, req.model_dump(), lambda *_: None)
        except RestorationError as exc:
            s.refresh_manifest()
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, KeyError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc


class ScenarioShockIn(BaseModel):
    source: str
    source_period: str | None = None
    pct: float


class ScenarioMetricIn(BaseModel):
    target: str | None = None
    metric: str | None = None
    period: str | None = None


class ScenarioPrepareIn(BaseModel):
    shocks: list[ScenarioShockIn] = Field(min_length=1, max_length=8)
    metrics: list[ScenarioMetricIn] = Field(min_length=1, max_length=8)
    experiment_reason: str | None = None


class ScenarioExecuteIn(BaseModel):
    plan_id: str
    compare_individual: bool = False


def _excel_scenario_call(session_id, fn, args):
    from .logbus import Cancelled
    from .workbook.transaction import RestorationError

    s = _session(session_id)
    with s.lock:
        try:
            return fn(s, args, lambda *_: None)
        except Cancelled as exc:
            raise HTTPException(409, str(exc)) from exc
        except RestorationError as exc:
            s.refresh_manifest()
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, KeyError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc


@app.post("/api/excel-scenarios/{session_id}/prepare")
def prepare_scenario(session_id: str, req: ScenarioPrepareIn):
    from .agent.tools_scenario import prepare_excel_scenario
    return _excel_scenario_call(session_id, prepare_excel_scenario, req.model_dump())


@app.post("/api/excel-scenarios/{session_id}/execute")
def execute_scenario(session_id: str, req: ScenarioExecuteIn):
    from .agent.tools_scenario import execute_excel_scenario
    return _excel_scenario_call(session_id, execute_excel_scenario, req.model_dump())


@app.get("/api/excel-scenarios/{session_id}")
def verified_scenarios(session_id: str, plan_id: str | None = None):
    from .agent.tools_scenario import get_verified_excel_scenario
    return _excel_scenario_call(session_id, get_verified_excel_scenario, {"plan_id": plan_id})


@app.get("/api/audit/{session_id}")
def model_audit(session_id: str):
    """Предпроверка без LLM (кнопка в интерфейсе)."""
    return _direct(session_id, "audit_model")


@app.post("/api/reset/{session_id}")
def reset(session_id: str):
    s = SESSIONS.get(session_id)
    if s:
        s.history, s.plan = [], None
    return {"ok": True}


@app.get("/api/params_xlsx/{session_id}")
def params_xlsx(session_id: str):
    """Реестр параметров модели — в Excel."""
    from .workbook import params as pr
    s = _session(session_id)
    path = os.path.join(s.workdir, "parameters.xlsx")
    pr.to_xlsx(s, path)
    return FileResponse(path, filename="parameters_" + os.path.splitext(s.card["file"])[0] + ".xlsx")


@app.get("/api/export/{session_id}")
def export(session_id: str):
    """Все стресс-прогоны сессии в Excel: лист на прогон, строки — метрика × сценарий."""
    s = SESSIONS.get(session_id)
    if s is None or not s.results:
        raise HTTPException(404, "Нет результатов стресс-тестов")
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for i, res in enumerate(s.results, 1):
        ws = wb.create_sheet(f"Прогон {i}")
        ws.append(["Сценарии"])
        ws["A1"].font = Font(bold=True)
        for sc in res["scenarios"]:
            ws.append([sc["name"], "; ".join(sc["shocks"])])
        ws.append([])
        for m in res["metrics"]:
            ws.append([f"{m['name']} ({m['ref']})", m["unit"]] + m["years"])
            ws.cell(ws.max_row, 1).font = Font(bold=True)
            for ser in m["series"]:
                ws.append([ser["name"], ""] + ser["values"])
            ws.append([])
        ws.column_dimensions["A"].width = 42
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=stress_results.xlsx"})


@app.on_event("startup")
def startup():
    from .logbus import BUS
    BUS.log(f"Сервер запущен. Рабочая папка (копии моделей и кэш): {config.workdir()}", "app")


@app.on_event("shutdown")
def shutdown():
    for s in SESSIONS.values():
        s.close()
