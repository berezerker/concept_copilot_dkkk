from .tools_ext import TOOLS_EXT
from .tools_credit import TOOLS_CREDIT
from .tools_expert import EXPERT_TOOL
from .tools_probe import TOOLS_PROBE
from .tools_semantic import TOOLS_SEMANTIC

from .tools import TOOLS, resolve_concepts


def _with_catalog(fn):
    """Единая точка входа инструментов: показатели справочника (concept) в метриках и ковенантах заменяются
    на строки модели до вызова инструмента — так concept понимают все инструменты, а не только run_stress."""
    def call(s, args, emit):
        if getattr(s, "catalog", None) and isinstance(args, dict) and "concept" in str(args):
            args = resolve_concepts(s, args)
        return fn(s, args, emit)
    call.__wrapped__ = fn
    return call


ALL_TOOLS = [dict(t, fn=_with_catalog(t["fn"]))
             for t in TOOLS + TOOLS_EXT + TOOLS_CREDIT + TOOLS_SEMANTIC + TOOLS_PROBE + [EXPERT_TOOL]]
TOOL_MAP = {t["name"]: t for t in ALL_TOOLS}


def tool_specs():
    import copy
    specs = [{"name": t["name"], "description": t["description"], "parameters": copy.deepcopy(t["parameters"])} for t in ALL_TOOLS]
    for spec in specs:
        spec["parameters"].setdefault("properties", {})["retry_of"] = {
            "type": "string", "description": "id ошибочного вызова этого же инструмента, который исправляет данный повтор с новыми аргументами"}
    return specs
