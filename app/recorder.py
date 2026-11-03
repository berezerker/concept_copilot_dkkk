"""Запись сеанса работы для интерактивного демо (CF_RECORD=1).

Каждый запрос к /api/* (кроме потока журнала) сохраняется в workspace/demo_rec/<сеанс>/NNN.json:
метод, путь, тело запроса, ответ по кускам с временем от начала (SSE приходит как в жизни),
а также записи журнала расчётов, появившиеся за время запроса. Из этих файлов tools/build_demo.py
собирает один demo.html: настоящий интерфейс + проигрывание записи без сервера, Excel и LLM.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time

_lock = threading.Lock()
_counter = [0]


def rec_dir():
    from . import config
    d = os.environ.get("CF_RECORD_DIR") or os.path.join(config.workdir(), "demo_rec", time.strftime("%Y%m%d_%H%M%S"))
    os.environ["CF_RECORD_DIR"] = d
    os.makedirs(d, exist_ok=True)
    return d


def _scrub(s: str) -> str:
    home = os.path.expanduser("~")
    return s.replace(home, "~") if home and len(home) > 1 else s


class RecorderMiddleware:
    def __init__(self, app):
        self.app = app
        self.dir = rec_dir()

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or path.startswith("/api/logs") \
                or path.startswith("/api/cancel"):
            return await self.app(scope, receive, send)
        from .logbus import BUS
        t0 = time.time()
        seq0 = BUS.seq
        body = bytearray()

        async def recv():
            msg = await receive()
            if msg["type"] == "http.request":
                body.extend(msg.get("body", b""))
            return msg

        rec = {"method": scope["method"], "path": path, "query": scope.get("query_string", b"").decode(),
               "status": 200, "headers": {}, "chunks": []}

        async def snd(msg):
            if msg["type"] == "http.response.start":
                rec["status"] = msg["status"]
                rec["headers"] = {k.decode(): v.decode() for k, v in msg.get("headers", [])
                                  if k.decode().lower() in ("content-type", "content-disposition")}
            elif msg["type"] == "http.response.body":
                b = msg.get("body", b"")
                if b:
                    ct = rec["headers"].get("content-type", "")
                    if ct.startswith(("application/json", "text/")):
                        rec["chunks"].append([round(time.time() - t0, 3), _scrub(b.decode("utf-8", "replace"))])
                    else:
                        rec["chunks"].append([round(time.time() - t0, 3), "b64:" + base64.b64encode(b).decode()])
            await send(msg)

        try:
            await self.app(scope, recv, snd)
        finally:
            ct = rec["headers"].get("content-type", "")
            if "multipart" in (dict((k.decode(), v.decode()) for k, v in scope.get("headers", [])).get("content-type", "")):
                rec["body"] = None                       # сам файл модели не пишем
            else:
                rec["body"] = _scrub(body.decode("utf-8", "replace")) if body else None
            rec["seconds"] = round(time.time() - t0, 2)
            rec["logs"] = [[round(x["t"] - t0, 3), {k: (_scrub(v) if isinstance(v, str) else v) for k, v in x.items()}]
                           for x in list(BUS.buf) if x["seq"] > seq0]
            with _lock:
                _counter[0] += 1
                n = _counter[0]
            with open(os.path.join(self.dir, f"{n:04d}.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
