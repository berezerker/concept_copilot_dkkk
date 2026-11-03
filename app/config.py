"""Настройки из переменных окружения и файла .env (без внешних зависимостей)."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path: str | None = None):
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def env_bool(name, default=False):
    v = env(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on", "да")


_WORKDIR = None


def _mac_excel_box():
    """Папка внутри «песочницы» Excel для Mac: файлы отсюда Excel открывает без запросов доступа."""
    import platform
    if platform.system() != "Darwin":
        return None
    base = os.path.expanduser("~/Library/Containers/com.microsoft.Excel/Data")
    if not os.path.isdir(base):
        return None
    d = os.path.join(base, "cf_agent_workspace")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return d
    except Exception:
        return None


def workdir():
    """Рабочая папка с копиями моделей и кэшем. На Mac с Excel — внутри песочницы Excel (без запросов доступа),
    иначе — ./workspace. CF_WORKDIR задаёт явно."""
    global _WORKDIR
    if _WORKDIR is None:
        d = env("CF_WORKDIR")
        # Папка-песочница Excel (_mac_excel_box) проверена на Excel 16.114: открытие оттуда не работает (-50),
        # поэтому по умолчанию — ./workspace, а запрос доступа Excel показывает один раз на модель.
        _WORKDIR = d or os.path.join(ROOT, "workspace")
        os.makedirs(_WORKDIR, exist_ok=True)
    return _WORKDIR


def cache_dir():
    d = os.path.join(workdir(), "_index_cache")
    os.makedirs(d, exist_ok=True)
    return d
