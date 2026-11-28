"""Отраслевые и кредитные проверки для свободного запроса агента."""
from ..workbook import credit_review


def get_credit_review(s, args, emit):
    data = credit_review.inspect(s)
    key = args.get("check_id")
    if key:
        check = next((check for check in data["checks"] if check["id"] == key), None)
        if check is None:
            raise ValueError("Проверка отсутствует в выбранных профилях книги")
        return {"version": data["version"], "check": check}
    order = {"inactive_only": 0, "review": 1, "source_detail_unavailable": 2, "limited": 3,
             "confirm_applicability": 4, "found": 5, "mapped": 6, "not_applicable": 7}
    checks = sorted(data["checks"], key=lambda item: order[item["status"]])
    return {"version": data["version"], "model": data["model"], "baseline": data["baseline"],
            "profiles": [profile for profile in data["profiles"] if profile["included"]],
            "checks": [{"id": check["id"], "title": check["title"], "status": check["status"],
                        "source": check["source"]} for check in checks],
            "confirmed_covenant": data["confirmed_covenant"],
            "reconciliations": [{"id": item["id"], "name": item["name"], "periods": list(item["periods"])}
                                for item in data["reconciliations"]]}


TOOLS_REVIEW = [
    {"name": "get_credit_review", "fn": get_credit_review,
     "description": "Показать применённые профили модели, отраслевые проверки и отмеченные пробелы. "
                    "По check_id вернуть признаки, ячейки, источники и уже измеренные эффекты. Пересчёта нет.",
     "parameters": {"type": "object", "properties": {"check_id": {"type": "string"}}}},
    {"name": "get_confirmed_covenant_result",
     "fn": lambda s, args, emit: credit_review.covenant_result(s, args.get("plan_id")),
     "description": "Проверить подтверждённый пользователем ковенант по живой базе Excel или проверенному "
                    "сценарию plan_id. Сверяет точные ячейки и охват всех выбранных периодов.",
     "parameters": {"type": "object", "properties": {"plan_id": {"type": "string"}}}},
    {"name": "reconcile_credit_metric", "fn": lambda s, args, emit: credit_review.reconcile(s, args),
     "description": "Сверить строку EBITDA с EBIT и амортизацией либо DSCR с CFADS и обслуживанием долга. "
                    "Сопоставимость, определение и знак третьей статьи должны быть подтверждены.",
     "parameters": {"type": "object", "properties": {
         "definition": {"type": "string", "enum": ["ebitda", "dscr"]},
         "period": {"type": "string"},
         "refs": {"type": "array", "items": {"type": "string"},
                  "description": "Три адреса A1: проверяемый показатель, первый и второй компоненты"},
         "sign": {"type": "integer", "enum": [-1, 1]},
         "tolerance": {"type": "number"},
         "source": {"type": "string", "description": "Основание выбранного определения"},
         "comparable_basis": {"type": "boolean", "description": "Подтверждены валюта, масштаб и периметр"},
     }, "required": ["definition", "period", "refs", "sign", "tolerance", "source", "comparable_basis"]}},
]
