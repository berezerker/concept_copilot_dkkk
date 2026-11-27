"""Фиксированный сценарий «Заключение для КПКИ»: одинаковый набор проверок для каждой модели.

Шаги идут всегда в одном порядке (воспроизводимо), LLM только пишет резюме, риски и рекомендации
по готовым цифрам. Без LLM (офлайн) резюме собирается по шаблону.

  1. Предпроверка модели            audit_model
  2. Ковенанты в базе               covenant_check
  3. От чего зависит устойчивость   dependency_analysis (экономические факторы, сцепки, концентрация риска)
  4. Стресс-пакет банка             stress_pack (топ-3 рыночных фактора, умеренный/жёсткий/комбинированный)
  5. Запас прочности                headroom (топ-4 фактора)
  6. Обратный стресс-тест           reverse_stress (топ-3 фактора, σ = 10%)
  7. Вероятность пробоя             monte_carlo (топ-3 фактора, σ = 10%)
  8. Заключение                     LLM / шаблон
"""
from __future__ import annotations

import datetime as dt
import json
import time

from ..workbook import semantics as sm
from ..workbook.transaction import RestorationError
from .registry import TOOL_MAP
from .tools import rnd

STEPS = ["Предпроверка модели", "Ковенанты в базовом сценарии", "От чего зависит устойчивость (факторы)",
         "Стресс-пакет банка (умеренный, жёсткий, комбинированный)", "Запас прочности по драйверам",
         "Обратный стресс-тест", "Вероятность пробоя (Монте-Карло)", "Заключение"]

PACK_CAT = {"price_sales": "price", "tariff": "price", "revenue": "revenue", "volume": "volume", "opex": "opex",
            "capex": "capex", "rate": "rate", "fx": "fx"}

CONCLUSION_SYSTEM = """Ты — старший кредитный аналитик ДККК. По результатам автоматических проверок финансовой модели
заёмщика напиши для Кредитного подкомитета (КПКИ) три раздела в markdown, по-русски, деловым стилем:
## Резюме — 3–5 предложений: устойчивость денежного потока, выполнение ковенантов, главный риск, общий вывод.
## Ключевые риски — 3–5 пунктов с цифрами из проверок (драйвер, запас прочности, сценарий пробоя, вероятность).
## Рекомендации — 3–5 пунктов: ковенанты/пороги, условия (cash sweep, резервный счёт, хеджирование, гарантии),
что запросить у клиента и что проверить в модели (по предпроверке).
Используй ТОЛЬКО цифры из переданных данных. Не придумывай названия, суммы и сроки. Не повторяй таблицы.
Говори языком экономических факторов и понятных названий показателей (цены реализации, курс, ставка, объёмы, долг),
а не адресами ячеек. Опирайся на раздел «dependencies» (доли факторов, концентрация риска, сцепки).
Факты и гипотезы раздельно: утверждай только то, что посчитали инструменты (цифры, доли, пробои). Выводы сверх расчёта помечай «гипотеза:» / «стоит проверить:». Если чувствительность метрики ко всем факторам равна 0 или инструмент вернул ошибку — прямо так и напиши, не подменяй расчёт экспертными рассуждениями. Сцепки факторов — это связи формул, а не доказательство цикла или концентрации риска."""


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:,.4g}".replace(",", " ") if abs(v) >= 1000 else f"{rnd(v, 4)}".replace(".", ",")
    return str(v)


def run_kpki(s, client, opts, emit):
    rules = [r for r in (opts.get("rules") or []) if r.get("sheet") and r.get("row")]
    if not rules:
        raise ValueError("Укажите хотя бы один ковенант (метрика, условие, порог)")
    for r in rules:
        r["row"] = int(r["row"])
        r["threshold"] = float(r["threshold"])
    s.client = client
    plan = {"goal": "Заключение для КПКИ по модели " + s.card["file"], "steps": [{"title": t, "status": "pending", "note": ""} for t in STEPS]}
    emit("plan", plan)
    R = {}

    def step(i, name, args, note_fn=None):
        plan["steps"][i]["status"] = "in_progress"
        emit("plan", plan)
        cid = f"kpki{i}"
        emit("tool_start", {"id": cid, "name": name, "args": args})
        t0 = time.time()
        error = None
        try:
            res = TOOL_MAP[name]["fn"](s, args, emit)
        except Exception as e:
            error, res = e, {"error": f"{type(e).__name__}: {e}"}
        ok = error is None
        emit("tool_end", {"id": cid, "name": name, "ok": ok, "seconds": round(time.time() - t0, 1), "result": res})
        plan["steps"][i]["status"] = "done" if ok else "skipped"
        if ok and note_fn:
            try:
                plan["steps"][i]["note"] = note_fn(res)
            except Exception:
                pass
        elif not ok:
            plan["steps"][i]["note"] = res["error"][:160]
        emit("plan", plan)
        if isinstance(error, RestorationError):
            raise error
        R[name] = res if ok else None
        return res if ok else None

    step(0, "audit_model", {}, lambda r: f"{r['total']} замечаний")
    cov = step(1, "covenant_check", {"rules": rules},
               lambda r: "; ".join(f"{x['rule']}: {'пробой ' + ', '.join(x['breach_years']) if x['breach_years'] else 'выполняется'}" for x in r["rules"]))
    main = rules[0]
    how = "min" if main.get("op", ">=") in (">=", ">") else "max"
    dep = step(2, "dependency_analysis", {"rules": rules[:2]},
               lambda r: ", ".join(f"{x['name']} {x['share'] * 100:.0f}%" for x in r["tornados"][0]["factors"][:3] if x.get("share")))
    top = []
    if dep and dep.get("tornados"):
        good = main.get("op", ">=") in (">=", ">")
        for x in dep["tornados"][0]["factors"]:
            if not x.get("market") or not x.get("delta"):
                continue
            hurt_down = (x["delta"] < 0) if good else (x["delta"] > 0)     # падение фактора ухудшает ковенант?
            fid = next((f for f, v in sm.FACTORS.items() if v[0] == x["name"]), None)
            if not fid:
                continue
            top.append({"factor": fid, "name": x["name"], "direction": -1 if hurt_down else 1})
            if len(top) >= 4:
                break
    if top:
        pack_drivers = []
        for d in top[:3]:
            sg = d["direction"]
            pack_drivers.append({"factor": d["factor"], "name": d["name"], "category": PACK_CAT.get(d["factor"], "other"),
                                 "op": "pct", "value": 10 * sg, "severe_value": 20 * sg})
        step(3, "stress_pack", {"drivers": pack_drivers, "rules": rules},
             lambda r: f"пробой в {len(r['breach_scenarios'])} из {len(r['rows']) - 1} сценариев")
        step(4, "headroom", {"rule": main, "drivers": [{"factor": d["factor"], "name": d["name"]} for d in top[:4]]},
             lambda r: "; ".join(f"{x['driver'][:20]}: {x['change_pct']}%" for x in r["drivers"] if x.get("change_pct") is not None))
        step(5, "reverse_stress", {"rules": rules, "drivers": [dict(factor=d["factor"], name=d["name"],
                                                                    direction=d["direction"], sd_pct=10) for d in top[:3]]},
             lambda r: f"пробой при совместном шоке ×{r['multiple']} ({r.get('plausibility') or ''})" if r.get("breach_found") else (r.get("message") or "")[:120])
        step(6, "monte_carlo", {"drivers": [dict(factor=d["factor"], name=d["name"], sd_pct=10) for d in top[:3]],
                                "metric": {"sheet": main["sheet"], "row": main["row"], "name": main.get("name")},
                                "aggregate": how, "covenant_op": main.get("op", ">="), "threshold": main["threshold"],
                                "n": int(opts.get("mc_runs", 80))},
             lambda r: f"вероятность пробоя {r['breach_probability'] * 100:.1f}%" if r.get("breach_probability") is not None else "")
    else:
        for i in (3, 4, 5, 6):
            plan["steps"][i]["status"] = "skipped"
            plan["steps"][i]["note"] = "нет влияющих рыночных факторов"
        emit("plan", plan)

    plan["steps"][7]["status"] = "in_progress"
    emit("plan", plan)
    body = report_body(s, rules, R)
    facts = compact_facts(s, rules, R)
    head = None
    if client is not None and getattr(client, "name", "") != "mock":
        emit("status", {"text": "LLM пишет резюме и рекомендации…"})
        try:
            resp = client.chat([{"role": "system", "content": CONCLUSION_SYSTEM},
                                {"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)}], [], "auto")
            head = (resp.content or "").strip() or None
        except Exception as e:
            emit("status", {"text": f"LLM недоступна ({e}); резюме по шаблону"})
    if not head:
        head = template_head(facts)
    plan["steps"][7]["status"] = "done"
    emit("plan", plan)
    title = f"# Заключение по финансовой модели для КПКИ\n\n**Модель:** {s.card['file']} · **дата:** {dt.date.today():%d.%m.%Y} · " \
            f"**прогноз с** {s.forecast_start} · **подготовлено:** Копилот ДККК ({client.describe() if client else 'офлайн'})\n"
    md = title + "\n" + head + "\n\n" + body + \
        "\n\n_Расчёты выполнены пересчётом копии модели; исходный файл не изменялся. Цифры — из модели заёмщика " \
        "и автоматических проверок; выводы требуют проверки кредитным аналитиком._"
    emit("answer", {"text": md, "report": True})
    return md


def compact_facts(s, rules, R):
    f = {"model": s.card["file"], "forecast_start": s.forecast_start, "rules": [f"{r.get('name')} {r.get('op')} {r['threshold']}" for r in rules]}
    if R.get("audit_model"):
        a = R["audit_model"]
        f["audit"] = {"total": a["total"], "summary": a["summary"],
                      "critical": [x["text"][:160] + " · " + x["where"] for x in a["findings"] if x["level"] == "critical"][:5]}
    if R.get("covenant_check"):
        f["covenants_base"] = [{"rule": x["rule"], "breach": x["breach_years"], "worst": x["worst"]} for x in R["covenant_check"]["rules"]]
    if R.get("dependency_analysis"):
        d = R["dependency_analysis"]
        f["dependencies"] = {"factors": [{"metric": t["metric"], "factors": [{k: x.get(k) for k in ("name", "share", "delta_pct", "market", "examples")}
                                                                             for x in t["factors"][:6]]} for t in d["tornados"]],
                             "findings": d["findings"], "couplings": [{"name": c["name"], "from": c["from"], "notes": c["notes"]} for c in d["couplings"][:5]]}
    if R.get("stress_pack"):
        f["stress_pack"] = [{"scenario": r["scenario"], "cells": r.get("cells")} for r in R["stress_pack"]["rows"]]
    if R.get("headroom"):
        f["headroom_pct"] = R["headroom"]["drivers"]
    if R.get("reverse_stress"):
        f["reverse_stress"] = {k: R["reverse_stress"].get(k) for k in ("multiple", "shocks", "binding", "plausibility", "message")}
    if R.get("monte_carlo"):
        f["monte_carlo"] = {k: R["monte_carlo"].get(k) for k in ("runs", "p5", "p50", "p95", "breach_probability")}
    return f


def template_head(f):
    lines = ["## Резюме"]
    cov = f.get("covenants_base") or []
    br = [c for c in cov if c["breach"]]
    lines.append(("В базовом сценарии ковенанты выполняются. " if not br else
                  "В базовом сценарии есть нарушения: " + "; ".join(f"{c['rule']} в {', '.join(c['breach'])}" for c in br) + ". "))
    if f.get("headroom_pct"):
        h = [x for x in f["headroom_pct"] if x.get("change_pct") is not None]
        if h:
            m = min(h, key=lambda x: abs(x["change_pct"]))
            lines[-1] += f"Наименьший запас прочности — по драйверу «{m['driver']}»: {_pc(m['change_pct'])} до пробоя. "
    if f.get("monte_carlo") and f["monte_carlo"].get("breach_probability") is not None:
        lines[-1] += f"Вероятность пробоя при волатильности ключевых драйверов ±10% — {f['monte_carlo']['breach_probability'] * 100:.0f}%."
    lines += ["", "## Ключевые риски"]
    for x in ((f.get("dependencies") or {}).get("findings") or [])[:4]:
        lines.append(f"- {x}")
    if f.get("reverse_stress") and f["reverse_stress"].get("shocks"):
        lines.append("- Обратный стресс: пробой при совместном изменении " + ", ".join(
            f"{x['driver']} {_pc(x['change_pct'])}" for x in f["reverse_stress"]["shocks"]) + f" ({f['reverse_stress'].get('plausibility') or ''}).")
    lines += ["", "## Рекомендации", "- Сверить драйверы с консервативным сценарием Аналитического хаба.",
              "- Устранить замечания предпроверки (раздел 1) до вынесения на КПКИ.",
              "- _Резюме собрано по шаблону без LLM; подключите OpenAI или GigaChat для экспертного текста._"]
    return "\n".join(lines)


def _pc(v):
    return "—" if v is None else f"{v:+g}%".replace(".", ",")


def _hd(x):
    if x.get("change_pct") is not None:
        return _pc(x["change_pct"])
    return x.get("note") or x.get("error") or "—"


def report_body(s, rules, R):
    out = []
    a = R.get("audit_model")
    out.append("## 1. Качество модели (предпроверка)")
    if a:
        out.append(f"Всего замечаний: **{a['total']}**. " + "; ".join(f"{k}: {v}" for k, v in a["summary"].items()))
        crit = [x for x in a["findings"] if x["level"] in ("critical", "warning")][:8]
        if crit:
            out += ["", "| Уровень | Где | Что |", "|---|---|---|"] + [
                f"| {'критично' if x['level'] == 'critical' else 'важно'} | `{x['where']}` | {x['text'][:140]} |" for x in crit]
    else:
        out.append("Предпроверка не выполнена.")
    c = R.get("covenant_check")
    out += ["", "## 2. Ковенанты в базовом сценарии"]
    if c:
        out += ["| Ковенант | Ячейка | Худшее значение | Период | Запас | Пробой |", "|---|---|---|---|---|---|"]
        for x in c["rules"]:
            w = x["worst"] or {}
            out.append(f"| {x['rule']} | `{x['ref']}` | {_fmt(w.get('value'))} | {w.get('year', '—')} | {_fmt(w.get('headroom_pct'))}% | "
                       f"{', '.join(map(str, x['breach_years'])) or 'нет'} |")
    d = R.get("dependency_analysis")
    out += ["", "## 3. От чего зависит устойчивость"]
    if d and d.get("tornados"):
        for t in d["tornados"]:
            out += ["", f"**{t['metric']}** (база {_fmt(t['base'])}) — изменение метрики при снижении фактора на 10%:", "",
                    "| Фактор | Доля чувствительности | Δ метрики, % | Примеры допущений |", "|---|---|---|---|"]
            for x in t["factors"][:7]:
                if x.get("share") is None:
                    continue
                kind = "" if x.get("market") else " _(структурный)_"
                out.append(f"| {x['name']}{kind} | {x['share'] * 100:.0f}% | {_pc(x.get('delta_pct'))} | {'; '.join(x.get('examples') or [])[:110]} |")
        if d.get("findings"):
            out += ["", "**Выводы:**"] + [f"- {x}" for x in d["findings"]]
        cps = d.get("couplings") or []
        if cps:
            out += ["", "**Сцепки факторов (найдены пересчётом модели):**", "",
                    "| Показатель | Зависит от | Что это значит | Цепочка |", "|---|---|---|---|"]
            for c in cps[:8]:
                out.append(f"| {c['name'][:60]} | {', '.join(c['from'])} | {c['note']} | {' → '.join(c.get('chain') or [])[:140]} |")
    else:
        out.append("Анализ факторов не выполнен.")
    p = R.get("stress_pack")
    if p:
        out += ["", "## 4. Стресс-пакет банка", "| Сценарий | " + " | ".join(f"{c['metric']} ({c['rule'] or c['aggregate']})" for c in p["columns"]) + " |",
                "|---|" + "---|" * len(p["columns"])]
        for r in p["rows"]:
            if r.get("error"):
                out.append(f"| {r['scenario']} | ошибка: {r['error'][:60]} |")
                continue
            cells = []
            for cl in r["cells"]:
                mark = "" if cl.get("ok") is None else (" ✓" if cl["ok"] else " **✗**")
                cells.append(f"{_fmt(cl['value'])} ({cl['period']}){mark}")
            out.append(f"| {r['scenario']} | " + " | ".join(cells) + " |")
    h = R.get("headroom")
    if h:
        out += ["", f"## 5. Запас прочности · {h['rule']} (база {_fmt(h['base'])})", "| Драйвер | Изменение до пробоя |", "|---|---|"] + [
            f"| {x['driver']} | {_hd(x)} |"
            for x in h["drivers"]]
    rv = R.get("reverse_stress")
    if rv:
        out += ["", "## 6. Обратный стресс-тест"]
        if rv.get("breach_found"):
            out.append("Минимальное совместное неблагоприятное изменение драйверов (в пропорциях волатильности σ = 10%), при котором нарушается ковенант:")
            out += ["", "| Драйвер | Изменение | σ |", "|---|---|---|"] + [f"| {x['driver']} | {_pc(x['change_pct'])} | {_fmt(x['sigma'])} |" for x in rv["shocks"]]
            out.append(f"\nСвязывающий ковенант: {'; '.join(rv['binding'])}. Правдоподобие: {rv.get('plausibility') or '—'}.")
        else:
            out.append(rv.get("message") or "")
    mc = R.get("monte_carlo")
    if mc:
        out += ["", "## 7. Вероятность пробоя (Монте-Карло)",
                f"{mc['runs']} прогонов, драйверы с σ = 10%: медиана {_fmt(mc['p50'])}, 90% интервал {_fmt(mc['p5'])} – {_fmt(mc['p95'])}, "
                f"**вероятность пробоя {mc['breach_probability'] * 100:.1f}%** ({mc['covenant']}).".replace(".0%", "%")]
    return "\n".join(out)
