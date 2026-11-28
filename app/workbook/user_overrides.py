"""Исправления привязок параметров для конкретной версии книги."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock


_write_lock = Lock()


def query_key(query):
    return " ".join(query.lower().replace("ё", "е").split())


def _path(session):
    cache = getattr(session, "cache_dir", None)
    return Path(cache) / f"user_overrides_{session.idx.file_hash}.json" if cache else None


def _read(path):
    if path is None:
        return {}
    try:
        bindings = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(bindings, dict) or any(
            not isinstance(item, dict)
            or any(not isinstance(item.get(field), str) or not item[field] for field in ("query", "parameter_id", "ref"))
            or key != query_key(item["query"])
            for key, item in bindings.items()
        ):
            raise ValueError("Некорректные привязки")
        return bindings
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ValueError("Пользовательские привязки параметров не удалось прочитать") from e


def load(session):
    return _read(_path(session))


def parameter_binding(session, query):
    return load(session).get(query_key(query)) if query else None


def set_parameter(session, query, parameter):
    """Закрепить запрос за параметром или удалить его привязку."""
    path = _path(session)
    key = query_key(query)
    if path is None or not key:
        raise ValueError("Для сохранения привязки нужны загруженная книга и непустой запрос")
    with _write_lock:
        bindings = _read(path)
        if parameter is None:
            bindings.pop(key, None)
        else:
            bindings[key] = {"query": query.strip(), "parameter_id": parameter["id"],
                             "ref": parameter["location"]["ref"]}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
                temporary = Path(f.name)
                json.dump(bindings, f, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return list(bindings.values())
