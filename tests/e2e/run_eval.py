"""E2E-прогон всего функционала без UI: те же функции, что вызывает сервер, + оценка ответов.

Примеры:
  python -m tests.e2e.run_eval --model ~/Documents/model.xlsx --provider openai --judge openai
  python -m tests.e2e.run_eval --model m1.xlsx --model m2.xlsx --cases stress_price,kpki
  python -m tests.e2e.run_eval --model m.xlsx --provider mock --no-judge      # быстрый smoke без LLM
  python -m tests.e2e.run_eval --model m.xlsx --direct-only                    # только кнопки (карта/аудит/факторы)

Отчёт: tests/e2e/reports/<время>/report.md + report.json (все ответы, трассы, оценки).
Код возврата: 1, если есть FAIL — удобно для автоматического прогона после правок.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402

config.load_dotenv(os.path.join(ROOT, ".env"))

from app.agent.harness import run_turn  # noqa: E402
from app.agent.registry import TOOL_MAP  # noqa: E402
from app.agent.session import Session  # noqa: E402
from app.llm import make_client  # noqa: E402
from app.logbus import BUS, ClientProxy  # noqa: E402
import re  # noqa: E402
from tests.e2e import grading  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class Recorder:
    """emit-функция: собирает события так же, как их увидел бы UI."""

    def __init__(self, verbose=False):
        self.tools, self.open, self.answer, self.error, self.events = [], {}, None, None, 0
        self.verbose = verbose

    def __call__(self, kind, data):
        self.events += 1
        if kind == "tool_start":
            self.open[data["id"]] = data
            if self.verbose:
                print(f"     → {data['name']}", flush=True)
        elif kind == "tool_end":
            st = self.open.pop(data["id"], {})
            self.tools.append({"name": data["name"], "args": st.get("args", {}), "ok": data.get("ok", True),
                               "seconds": data.get("seconds"), "result": data.get("result")})
            if not data.get("ok", True):
                err = data.get("result")
                err = err.get("error", err) if isinstance(err, dict) else err
                print(f"       ✗ {data['name']}: {str(err)[:400]}", flush=True)
        elif kind == "answer":
            self.answer = (self.answer + "\n\n" if self.answer else "") + data.get("text", "")
            if data.get("complete") is False:
                self.error = "Harness не подтвердил завершение задачи"
        elif kind == "error":
            self.error = data.get("text")
        elif self.verbose and kind in ("status", "progress"):
            print(f"       {data.get('text', '')[:110]}", flush=True)


def defaults(s):
    """То же, что /api/defaults: ковенанты по карточке и поля-драйверы."""
    from app.server import DEFAULT_RULES
    from app.workbook import semantics as sm
    km = {m["metric"]: m["candidates"] for m in s.card["key_metrics"]}
    rules = [{"sheet": c["sheet"], "row": c["row"], "name": n, "op": op, "threshold": thr, "label": c["label"]}
             for n, op, thr in DEFAULT_RULES for c in km.get(n, [])[:1]]
    metrics = [{"sheet": c["sheet"], "row": c["row"], "name": m} for m, cs in km.items() for c in cs[:1]]
    fields = [{"sheet": d["sheet"], "row": d["row"], "name": sm.name(s, d["sheet"], d["row"])[:60], "unit": d["unit"]}
              for d in s.card["drivers"][:15]]
    return rules, metrics, fields


def run_case(s, case, client, rec):
    from app.server import _ensure_sem, DEPS_SYSTEM
    kind = case["kind"]
    if kind == "agent":
        s.history = []
        if client.name != "mock":
            _ensure_sem(s, client, rec)
        run_turn(s, client, case["query"], rec)
    elif kind == "direct":
        t0 = time.time()
        res = TOOL_MAP[case["tool"]]["fn"](s, dict(case.get("args") or {}), rec)
        rec("tool_end", {"id": "d", "name": case["tool"], "ok": True, "seconds": round(time.time() - t0, 1), "result": res})
    elif kind == "deps_llm":
        _ensure_sem(s, client, rec)
        res = TOOL_MAP["dependency_analysis"]["fn"](s, {}, rec)
        rec("tool_end", {"id": "d", "name": "dependency_analysis", "ok": True, "result": res})
        if client.name != "mock":
            facts = {k: res.get(k) for k in ("tornados", "findings", "couplings", "industry")}
            r = client.chat([{"role": "system", "content": DEPS_SYSTEM},
                             {"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)}], [], "auto")
            rec("answer", {"text": (r.content or "").strip()})
    elif kind == "expert":
        rules, metrics, fields = defaults(s)
        s.client = client
        _ensure_sem(s, client, rec)
        res = TOOL_MAP["industry_expert"]["fn"](s, {"news": case["news"], "fields": fields, "metrics": metrics, "rules": rules}, rec)
        rec("tool_end", {"id": "e", "name": "industry_expert", "ok": True, "result": res})
        rec("answer", {"text": expert_md(res)})
    elif kind == "kpki":
        from app.agent.kpki import run_kpki
        rules, _, _ = defaults(s)
        _ensure_sem(s, client, rec)
        run_kpki(s, client, {"rules": rules, "mc_runs": case.get("mc_runs", 40), "max_candidates": 40}, rec)
    else:
        raise ValueError(f"неизвестный kind {kind}")


def expert_md(res):
    """Ответ эксперта так, как его видит пользователь в UI: допущения, итог стресса, ковенанты, предупреждения."""
    L = [f"**{res.get('title')}**", res.get("summary", ""), "", "Изменённые допущения:"]
    for a in res.get("assumptions") or []:
        ch = a.get("change") or (", ".join(f"{k}: {v:g}" for k, v in a["new"].items()) if isinstance(a.get("new"), dict) else "")
        L.append(f"- {a.get('field')}: {ch} — {a.get('rationale', '')}")
    for m in (res.get("stress") or {}).get("metrics", []):
        for sm_ in m.get("summary") or []:
            L.append(f"- Итог «{sm_['scenario']}» → {re.sub(r"^\S+!\d+ ", "", m['metric'])}: худшее {sm_.get('min')}, макс. отклонение {sm_.get('max_abs_delta')}")
    cov = res.get("covenants")
    if cov:
        L.append("Ковенанты: " + json.dumps(cov, ensure_ascii=False, default=str)[:1500])
    if res.get("key_risks"):
        L += ["Риски:"] + [f"- {r}" for r in res["key_risks"]]
    if res.get("warnings"):
        L += ["Предупреждения:"] + [f"- {w}" for w in res["warnings"]]
    return "\n".join(L)


def engine_stats(seq0):
    """Сколько пересчётов и сколько они заняли (по журналу движка) — где тратится время."""
    n, tot, parts = 0, 0.0, {}
    for x in list(BUS.buf):
        if x["seq"] <= seq0 or x["src"] != "engine" or not x["text"].startswith("пересчёт"):
            continue
        m = re.search(r"— ([\d.]+) с", x["text"])
        if m:
            n, tot = n + 1, tot + float(m.group(1))
        for k, v in re.findall(r"(запись|пересчёт|чтение|откат) ([\d.]+) с", x["text"]):
            parts[k] = round(parts.get(k, 0) + float(v), 1)
    return {"n": n, "t": round(tot, 1), "parts": parts}


def eng_txt(es):
    if not es["n"]:
        return ""
    return f" · пересчётов {es['n']} ({es['t']:.0f} с, {es['t'] / es['n']:.1f} с/шт: {es['parts']})"


def check_direct(case, rec):
    """Структурные проверки кнопок без LLM: результат не пустой и содержит ожидаемые разделы."""
    res = rec.tools[-1]["result"] if rec.tools else None
    need = {"model_map": ["sheets"], "audit_model": ["summary"], "dependency_analysis": ["tornados"]}.get(case.get("tool"), [])
    miss = [k for k in need if not (isinstance(res, dict) and res.get(k))]
    return miss


def main(argv=None):
    ap = argparse.ArgumentParser(description="E2E-тесты агента CashFlow с LLM-as-judge")
    ap.add_argument("--model", action="append", required=True, help="путь к .xlsx (можно несколько)")
    ap.add_argument("--provider", default=config.env("CF_EVAL_PROVIDER", "openai"), help="LLM агента: openai/gigachat/mock")
    ap.add_argument("--llm-model", default=None, help="имя модели LLM агента")
    ap.add_argument("--judge", default=config.env("CF_EVAL_JUDGE"), help="LLM судьи (по умолчанию = provider)")
    ap.add_argument("--judge-model", default=None)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--cases", default="", help="id через запятую (по умолчанию все)")
    ap.add_argument("--direct-only", action="store_true", help="только кнопки без LLM")
    ap.add_argument("--engine", default=config.env("CF_ENGINE"), help="excel / libreoffice / formulas")
    ap.add_argument("--cases-file", default=os.path.join(HERE, "cases.json"))
    ap.add_argument("--fresh", action="store_true", help="удалить кэш карты и смыслового слоя этих моделей перед прогоном")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

    cases = json.load(open(a.cases_file, encoding="utf-8"))["cases"]
    if a.cases:
        want = {c.strip() for c in a.cases.split(",")}
        cases = [c for c in cases if c["id"] in want]
    if a.direct_only:
        cases = [c for c in cases if c["kind"] == "direct"]
    client = ClientProxy(make_client(a.provider, a.llm_model))
    jclient = None
    if not a.no_judge and (a.judge or a.provider) != "mock":
        try:
            jclient = ClientProxy(make_client(a.judge or a.provider, a.judge_model))
        except Exception as e:
            print(f"Судья недоступен: {e}")
    if a.engine:
        os.environ["CF_ENGINE"] = a.engine

    out_dir = os.path.join(HERE, "reports", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for mpath in a.model:
        mpath = os.path.expanduser(mpath)
        mname = os.path.basename(mpath)
        print(f"\n━━ {mname} — загрузка…", flush=True)
        if a.fresh:
            from app.workbook.index import _file_hash
            fh = _file_hash(mpath)
            gone = [f for f in os.listdir(config.cache_dir()) if f.startswith(fh)]
            for f in gone:
                p = os.path.join(config.cache_dir(), f)
                os.remove(p) if os.path.isfile(p) else __import__("shutil").rmtree(p)
            print(f"   кэш модели очищен ({len(gone)} файлов) — карта и LLM-описания строятся заново", flush=True)
        s = Session(config.workdir())
        t0 = time.time()
        try:
            s.load(mpath, mname, config.cache_dir(), os.environ.get("CF_ENGINE"),
                   progress=None if a.quiet else (lambda t: print("   ", t, flush=True)))
        except Exception as e:
            print(f"   ✗ не загрузилась: {e}")
            results.append({"model": mname, "case": "load", "status": "FAIL", "reasons": [str(e)]})
            continue
        print(f"   загружено за {time.time() - t0:.1f} с; движок: {s.engine.name}"
              f"{'' if s.engine_open else ' (НЕ открыт: ' + str(s.engine_error) + ')'}", flush=True)
        for case in cases:
            print(f" ▸ {case['id']} ({case['kind']})", flush=True)
            rec = Recorder(verbose=not a.quiet)
            seq0 = BUS.seq
            t1 = time.time()
            try:
                run_case(s, case, client, rec)
            except BaseException as e:  # noqa — фиксируем и идём дальше
                if isinstance(e, KeyboardInterrupt):
                    raise
                rec.error = f"{type(e).__name__}: {e}"
                tb = traceback.format_exc(limit=4)
            else:
                tb = None
            run = {"tools": rec.tools, "answer": rec.answer, "error": rec.error, "seconds": round(time.time() - t1, 1)}
            det = grading.deterministic(case, run)
            det["engine"] = engine_stats(seq0)
            if case["kind"] == "direct" and not rec.error:
                miss = check_direct(case, rec)
                if miss:
                    det["error"] = f"в результате нет разделов {miss}"
            jd = grading.judge(jclient, case, run) if jclient else None
            status, reasons = grading.verdict(det, jd)
            g = (det.get("grounding") or {}).get("score")
            es = det["engine"]
            print(f"   {status:4} {run['seconds']:7.1f} с · шагов {det['steps']}"
                  f"{eng_txt(es)}"
                  f"{f' · числа {g:.0%}' if g is not None else ''}"
                  f"{' · судья ' + str(jd['score']) if jd and jd.get('score') else ''}"
                  f"{' · ' + '; '.join(reasons) if reasons else ''}", flush=True)
            results.append({"model": mname, "case": case["id"], "kind": case["kind"], "status": status, "reasons": reasons,
                            "det": det, "judge": jd, "answer": run["answer"], "error": run["error"], "traceback": tb,
                            "tools": [{k: t[k] for k in ("name", "args", "ok", "seconds")} | (
                                {"error": t["result"]} if not t["ok"] else {}) for t in rec.tools]})
            write_report(out_dir, results, a)          # после каждого кейса — отчёт не теряется при остановке
        try:
            s.engine.close()
        except Exception:
            pass

    write_report(out_dir, results, a)
    n_fail = sum(r["status"] == "FAIL" for r in results)
    print(f"\nИтого: {len(results)} · PASS {sum(r['status'] == 'PASS' for r in results)} · "
          f"WARN {sum(r['status'] == 'WARN' for r in results)} · FAIL {n_fail}\nОтчёт: {out_dir}/report.md")
    return 1 if n_fail else 0


def write_report(out_dir, results, a):
    json.dump(results, open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    L = [f"# E2E-отчёт {time.strftime('%Y-%m-%d %H:%M')}", "",
         f"Агент: `{a.provider}` · судья: `{'нет' if a.no_judge else (a.judge or a.provider)}` · движок: `{os.environ.get('CF_ENGINE', 'авто')}`", "",
         "| Модель | Кейс | Итог | Время, с | Шагов | Числа | Судья | Причины |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        d, j = r.get("det") or {}, r.get("judge") or {}
        g = (d.get("grounding") or {}).get("score")
        L.append(f"| {r['model'][:28]} | {r['case']} | **{r['status']}** | {d.get('seconds', '')} | {d.get('steps', '')} | "
                 f"{'' if g is None else f'{g:.0%}'} | {j.get('score', '')} | {'; '.join(r['reasons'])[:200]} |")
    L += ["", "## Детали"]
    for r in results:
        j = r.get("judge") or {}
        L += ["", f"### {r['model']} · {r['case']} — {r['status']}"]
        if r.get("tools"):
            L.append("Инструменты: " + " → ".join(f"{t['name']}{'' if t['ok'] else '✗'}" for t in r["tools"]))
        if r.get("error"):
            L += [f"**Ошибка:** {r['error']}", f"```\n{r.get('traceback') or ''}\n```"]
        if j and not j.get("error"):
            L.append("Судья: " + ", ".join(f"{k} {j.get(k)}" for k in grading.CRITERIA) + f" · pass={j.get('pass')}")
            for k, t in (("good", "Хорошо"), ("bad", "Плохо"), ("fix", "Что улучшить")):
                if j.get(k):
                    L.append(f"- *{t}:* {j[k]}")
        un = ((r.get("det") or {}).get("grounding") or {}).get("ungrounded")
        if un:
            L.append(f"Числа без подтверждения в расчётах: {un}")
        if r.get("answer"):
            L += ["", "<details><summary>Ответ</summary>", "", r["answer"], "", "</details>"]
    open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8").write("\n".join(L))
    with open(os.path.join(out_dir, "engine_llm.log"), "w", encoding="utf-8") as f:
        for x in list(BUS.buf):
            f.write(f"{time.strftime('%H:%M:%S', time.localtime(x['t']))} [{x['src']}] {x['text']}\n")


if __name__ == "__main__":
    sys.exit(main())
