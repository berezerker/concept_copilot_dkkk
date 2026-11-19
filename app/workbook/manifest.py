"""Единый паспорт произвольной Excel-модели.

Manifest объединяет уже построенные детерминированные представления книги:
справочник показателей, реестр изменяемых параметров, карту листов и наборы
сценариев.  Это контракт между анализатором книги, агентом и интерфейсом:
агент получает не россыпь адресов, а сущности с устойчивыми id, источниками
вывода, уверенностью и явным статусом готовности.

Manifest ничего не меняет в книге и не запускает пересчёт. Активные проверки
пересчётом (например, выбор набора допущений и direct_verified) добавляются в
evidence, когда они уже были выполнены соответствующими инструментами.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter

from . import catalog as cg
from . import params as pr
from .card_utils import numeric_years
from .modelmap import build_map


MANIFEST_VERSION = 1
_WORD = re.compile(r"[a-zа-я0-9]+", re.I)


def _norm(value: str) -> str:
    return " ".join(_WORD.findall((value or "").lower().replace("ё", "е")))


def parameter_id(sheet: str, row: int) -> str:
    """Устойчивый id параметра внутри одной версии книги; адрес остаётся отдельным полем."""
    raw = f"{sheet}\0{int(row)}".encode("utf-8")
    return "parameter:" + hashlib.sha1(raw).hexdigest()[:12]


def _metric_evidence(entry: dict) -> list[dict]:
    match = entry.get("match") or "structure"
    evidence = [{
        "kind": "user_override" if match == "pinned" else ("llm_mapping" if match == "llm" else "label_and_structure"),
        "status": "verified" if match == "pinned" else "inferred",
        "detail": match,
    }]
    if entry.get("live"):
        evidence.append({"kind": "formula_graph", "status": "supported",
                         "detail": f"{entry.get('inputs', 0)} upstream parameters"})
    if entry.get("direct_verified") is not None:
        evidence.append({"kind": "recalculation", "status": "verified",
                         "detail": "direct shock changes downstream results" if entry["direct_verified"]
                         else "direct shock does not change downstream results"})
    return evidence


def _metric_record(s, cid: str, entry: dict, param_entries: list[dict]) -> dict:
    primary, origin = entry["primary"], entry["origin"]
    periods = sorted(numeric_years(s.idx, primary["sheet"], primary["row"]))
    affected = [p for p in param_entries if entry["name"] in (p.get("influences") or [])]
    factors = Counter(p["factor_name"] for p in affected)
    identity = float(entry.get("confidence", 0.0) or 0.0)
    graph_conf = 0.9 if entry.get("live") else 0.35
    if any(s.idx.sheets[x["sheet"]].dynamic for x in affected if x["sheet"] in s.idx.sheets):
        graph_conf = min(graph_conf, 0.65)
    shock_conf = 0.95 if entry.get("direct_verified") is not None else (0.7 if entry.get("shock") == "drivers" else 0.6)
    status = "verified" if entry.get("match") == "pinned" or entry.get("direct_verified") is not None \
        else ("review" if identity < 0.6 or not entry.get("live") else "inferred")
    return {
        "id": f"metric:{cid}", "catalog_id": cid, "kind": "metric", "name": entry["name"],
        "group": cg.GROUPS.get(entry["group"], entry["group"]), "metric_kind": entry["kind"],
        "location": {"reported": f"{primary['sheet']}!{primary['row']}",
                     "computed": f"{origin['sheet']}!{origin['row']}",
                     "label": primary.get("label") or "", "unit": primary.get("unit") or ""},
        "periods": [str(x) for x in periods],
        "shock": {"method": entry.get("shock"), "direct_verified": entry.get("direct_verified"),
                  "reaches": list(entry.get("reaches") or []), "via": entry.get("via")},
        "dependencies": {"parameters": len(affected), "factors": dict(factors.most_common(10))},
        "confidence": {"identity": round(identity, 2), "dependency": graph_conf,
                       "shock": shock_conf, "unit": 0.9 if primary.get("unit") else 0.45},
        "status": status, "evidence": _metric_evidence(entry),
        "alternatives": [f"{x['sheet']}!{x['row']}" for x in entry.get("alternatives") or []],
        "note": entry.get("note"),
    }


def _parameter_record(s, entry: dict) -> dict:
    sheet, row = entry["sheet"], int(entry["row"])
    dynamic = bool(s.idx.sheets.get(sheet) and s.idx.sheets[sheet].dynamic)
    identity = 0.85 if (s.sem or {}).get("llm") else 0.68
    dependency = 0.65 if dynamic else (0.9 if entry.get("influences") else 0.55)
    shock = 0.72 if entry.get("mixed") else 0.95
    return {
        "id": parameter_id(sheet, row), "kind": "parameter", "name": entry["name"],
        "location": {"ref": entry["ref"], "sheet": sheet, "row": row, "label": entry["label"],
                     "block": entry.get("block") or "", "unit": entry.get("unit") or ""},
        "factor": {"id": entry["factor"], "name": entry["factor_name"], "sub": entry.get("sub")},
        "values": entry.get("values") or {}, "scalar": bool(entry.get("scalar")),
        "mixed": bool(entry.get("mixed")), "active": sheet not in s.idx.inactive,
        "impacts": list(entry.get("influences") or []), "fanout": entry.get("fanout", 0),
        "confidence": {"identity": identity, "dependency": dependency, "shock": shock,
                       "unit": 0.85 if entry.get("unit") else 0.4},
        "status": "review" if entry.get("mixed") or not entry.get("influences") else "inferred",
        "evidence": [{"kind": "typed_value", "status": "verified", "detail": "value is stored in workbook"},
                     {"kind": "formula_graph", "status": "supported" if entry.get("influences") else "unknown",
                      "detail": f"affects {len(entry.get('influences') or [])} catalog metrics"}],
        "description": entry.get("desc") or "",
    }


def _readiness(s, metrics: list[dict], parameters: list[dict], limitations: list[dict]) -> dict:
    low = [m for m in metrics if m["confidence"]["identity"] < 0.6 or m["status"] == "review"]
    unverified_sets = [a for a in s.catalog.get("alt_sets") or [] if not a.get("detected")]
    blockers = []
    if not metrics:
        blockers.append("Не найдено ни одного ключевого показателя")
    if not parameters:
        blockers.append("Не найдено изменяемых параметров, влияющих на расчёт")
    warnings = []
    if low:
        warnings.append(f"{len(low)} показателей требуют проверки")
    if unverified_sets:
        warnings.append(f"{len(unverified_sets)} наборов допущений не проверены пересчётом")
    if limitations:
        warnings.append(f"{len(limitations)} ограничений карты модели")
    if not s.engine_open:
        warnings.append("Движок пересчёта недоступен: возможен только анализ")
    if blockers:
        status = "blocked"
    elif not s.engine_open:
        status = "read_only"
    elif warnings:
        status = "ready_with_warnings"
    else:
        status = "ready"
    return {
        "status": status,
        "capabilities": {"analyze": bool(metrics),
                         "stress": bool(s.engine_open and metrics and parameters),
                         "free_agent": bool(metrics and parameters)},
        "coverage": {"catalog_metrics": len(metrics), "catalog_total": len(cg.CONCEPTS),
                     "parameters": len(parameters),
                     "parameters_with_known_impact": sum(bool(p["impacts"]) for p in parameters),
                     "low_confidence_metrics": len(low),
                     "verified_scenario_sets": sum(bool(a.get("detected")) for a in s.catalog.get("alt_sets") or []),
                     "scenario_sets": len(s.catalog.get("alt_sets") or [])},
        "semantic_layer": "llm" if (s.sem or {}).get("llm") else "rules",
        "engine": s.engine.name if s.engine_open else None,
        "blockers": blockers, "warnings": warnings,
    }


def build(s) -> dict:
    """Построить manifest из текущего состояния Session; без LLM и пересчётов."""
    psrc = pr.entries(s)
    parameters = [_parameter_record(s, p) for p in sorted(psrc, key=lambda x: (x["sheet"], x["row"]))]
    metrics = [_metric_record(s, cid, entry, psrc)
               for cid, entry in sorted(s.catalog["concepts"].items())]
    model_map = build_map(s.idx, s.card, s.forecast_start)
    zones = [dict(x, active=x["sheet"] not in s.idx.inactive) for x in model_map["sheets"]]
    scenario_sets = [{
        "id": f"scenario_set:{i + 1}", "sheets": list(a.get("sheets") or []),
        "active": list(a.get("active") or []), "verified": bool(a.get("detected")),
        "confidence": 0.95 if a.get("detected") else 0.55,
    } for i, a in enumerate(s.catalog.get("alt_sets") or [])]
    limitations = []
    for warning in list(dict.fromkeys((s.idx.warnings or []) + (s.catalog.get("warnings") or []))):
        limitations.append({"code": "incomplete_static_map", "severity": "warning", "message": warning})
    if s.catalog.get("missing"):
        limitations.append({"code": "missing_catalog_metrics", "severity": "info",
                            "message": "Не найдены типовые показатели: " + ", ".join(
                                cg.BY_ID[x]["name"] for x in s.catalog["missing"] if x in cg.BY_ID)})
    readiness = _readiness(s, metrics, parameters, limitations)
    return {
        "schema_version": MANIFEST_VERSION,
        "model": {"file": s.card["file"], "file_hash": s.idx.file_hash,
                  "sheets": len(s.idx.sheets), "formulas": s.idx.n_formulas,
                  "forecast_start": s.forecast_start, "iterative_calculation": bool(s.idx.iterate)},
        "readiness": readiness, "zones": zones,
        "edges": model_map["edges"], "metrics": metrics, "parameters": parameters,
        "scenario_sets": scenario_sets,
        "switches": [{"id": f"switch:{x['sheet']}!{x['row']}", **x} for x in s.card.get("switches") or []],
        "limitations": limitations,
        "provenance": {"catalog": "lexicon + structure + optional LLM mapping",
                       "parameters": "typed values referenced by formulas",
                       "dependencies": "static formula graph; dynamic links are listed in limitations"},
    }


def summary(manifest: dict) -> dict:
    """Компактная часть manifest для загрузки, UI и системного промпта."""
    factor_counts = Counter(p["factor"]["name"] for p in manifest["parameters"])
    return {
        "schema_version": manifest["schema_version"], "model": manifest["model"],
        "readiness": manifest["readiness"],
        "counts": {"metrics": len(manifest["metrics"]), "parameters": len(manifest["parameters"]),
                   "zones": len(manifest["zones"]), "edges": len(manifest["edges"]),
                   "scenario_sets": len(manifest["scenario_sets"]), "limitations": len(manifest["limitations"])},
        "key_metrics": [{"id": m["id"], "name": m["name"], "status": m["status"],
                         "confidence": m["confidence"]["identity"], "where": m["location"]["reported"],
                         "shock": m["shock"]["method"]} for m in manifest["metrics"][:15]],
        "factors": [{"name": name, "parameters": n} for name, n in factor_counts.most_common(15)],
        "scenario_sets": manifest["scenario_sets"],
        "limitations": manifest["limitations"][:8],
    }


def text(manifest: dict) -> str:
    r = manifest["readiness"]
    cov = r["coverage"]
    return (f"Статус: {r['status']}; показателей {cov['catalog_metrics']} из {cov['catalog_total']}, "
            f"параметров {cov['parameters']} ({cov['parameters_with_known_impact']} с известным влиянием), "
            f"показателей на проверку {cov['low_confidence_metrics']}; "
            f"анализ={'да' if r['capabilities']['analyze'] else 'нет'}, "
            f"стресс={'да' if r['capabilities']['stress'] else 'нет'}. "
            + ("Предупреждения: " + "; ".join(r["warnings"]) if r["warnings"] else ""))


def _metric_candidates(manifest: dict, query: str) -> list[tuple[float, dict]]:
    q = _norm(query)
    out = []
    for m in manifest["metrics"]:
        vals = [_norm(m["catalog_id"]), _norm(m["name"]), _norm(m["location"]["label"]), _norm(m["id"])]
        if query == m["id"] or q == _norm(m["catalog_id"]):
            score = 100.0
        elif q and q in vals:
            score = 20.0
        elif q and any(q in x or x in q for x in vals if x):
            score = 10.0
        else:
            qt = set(q.split())
            overlap = max((len(qt & set(x.split())) / max(1, len(qt)) for x in vals), default=0)
            if overlap < 0.5:
                continue
            score = 5.0 * overlap
        out.append((score + m["confidence"]["identity"], m))
    return sorted(out, key=lambda x: -x[0])


def resolve(s, query: str, kind: str = "auto", intent: str = "read", limit: int = 8) -> dict:
    """Разрешить слова пользователя в сущность manifest без угадывания при неоднозначности."""
    manifest = getattr(s, "manifest", None) or build(s)
    query = str(query or "").strip()
    if not query:
        raise ValueError("Нужно указать query — показатель или параметр словами пользователя")
    candidates = []
    if kind in ("auto", "metric"):
        for score, m in _metric_candidates(manifest, query)[:limit]:
            candidates.append({"score": round(score, 2), **m})
    if kind in ("auto", "parameter"):
        hits, _ = pr.search(s, query, limit=limit)
        by_ref = {p["location"]["ref"]: p for p in manifest["parameters"]}
        for hit in hits:
            p = by_ref.get(hit["ref"])
            if p:
                candidates.append({"score": round(float(hit.get("score", 0)), 2), **p})
    # Точный id или адрес всегда сильнее смыслового поиска.
    qnorm = _norm(query)
    exact = [x for x in candidates if query == x.get("id") or
             query.lower() == str((x.get("location") or {}).get("ref", "")).lower() or
             qnorm == _norm(x.get("name") or "") or
             qnorm == _norm((x.get("location") or {}).get("label") or "")]
    candidates.sort(key=lambda x: -x["score"])
    selected = exact[0] if exact else (candidates[0] if candidates else None)
    if exact:
        status = "resolved"
    elif not candidates:
        status = "not_found"
    elif len(candidates) == 1:
        status = "resolved"
    else:
        gap = candidates[0]["score"] - candidates[1]["score"]
        status = "resolved" if candidates[0]["score"] >= 15 or gap >= 2.0 else "ambiguous"
    warnings = []
    if selected and intent == "shock":
        if selected["kind"] == "metric" and selected.get("metric_kind") == "ratio":
            warnings.append("Коэффициент нельзя задавать напрямую; нужно менять его драйверы")
        if selected["kind"] == "parameter":
            if not selected.get("impacts"):
                warnings.append("Статическая карта не подтверждает влияние параметра на ключевые показатели")
            if selected.get("mixed"):
                warnings.append("Строка содержит и значения, и формулы; перед шоком нужен точный диапазон")
            if not selected["location"].get("unit"):
                warnings.append("Единица параметра не распознана; операцию и масштаб нужно проверить")
    return {
        "query": query, "kind": kind, "intent": intent, "status": status,
        "selected": selected if status == "resolved" else None,
        "candidates": candidates[:limit], "warnings": warnings,
        "next_action": "build_scenario" if status == "resolved" and not warnings else
                       ("inspect_and_confirm" if candidates else "search_model"),
        "note": "resolved означает однозначное сопоставление, но не заменяет preflight и проверку пересчётом",
    }
