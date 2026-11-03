from .tools import TOOLS
from .tools_ext import TOOLS_EXT
from .tools_credit import TOOLS_CREDIT
from .tools_expert import EXPERT_TOOL
from .tools_semantic import TOOLS_SEMANTIC

ALL_TOOLS = TOOLS + TOOLS_EXT + TOOLS_CREDIT + TOOLS_SEMANTIC + [EXPERT_TOOL]
TOOL_MAP = {t["name"]: t for t in ALL_TOOLS}


def tool_specs():
    return [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in ALL_TOOLS]
