"""Воркер пересчёта, работающий ВНУТРИ LibreOffice как Python-макрос.

Почему так: на macOS встроенный Python LibreOffice нельзя запускать сторонним процессом
(Launch Constraint), а модуль uno есть только в нём. Поэтому наш сервер запускает сам soffice
с этим макросом, а макрос поднимает локальный TCP-сервер и выполняет команды (JSON-строки):
  {"cmd":"open","path":...,"iterate":true}
  {"cmd":"read","ranges":[...]}
  {"cmd":"scenario","overrides":[{"ref","values"}],"watch":[...],"reads":[...]}
  {"cmd":"quit"}
Порт и токен передаются через файл cf_worker.json в профиле LibreOffice.
"""
import json
import os
import socket


def _pv(name, value):
    import uno  # noqa: F401
    from com.sun.star.beans import PropertyValue
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


class Worker:
    def __init__(self, desktop):
        self.desktop = desktop
        self.doc = None
        self.iterate = False

    def rng(self, ref):
        sheet, addr = ref.rsplit("!", 1)
        sheet = sheet.strip("'").replace("''", "'")
        return self.doc.Sheets.getByName(sheet).getCellRangeByName(addr.replace("$", ""))

    def read(self, refs):
        out = []
        for r in refs:
            data = self.rng(r).getDataArray()
            out.append([[v if v != "" else None for v in row] for row in data])
        return out

    def converge(self, watch, max_iter=40, tol=1e-6):
        prev = None
        for k in range(1, max_iter + 1):
            self.doc.calculateAll()
            cur = [v for block in self.read(watch) for row in block for v in row
                   if isinstance(v, (int, float))] if watch else []
            if prev is not None and len(cur) == len(prev) and all(
                    abs(a - b) <= tol * max(1.0, abs(b)) for a, b in zip(cur, prev)):
                return k
            prev = cur
            if not self.iterate and k >= 2:
                return k
        return max_iter

    def open(self, path, iterate=False):
        import uno
        if self.doc is not None:
            self.doc.close(True)
        url = uno.systemPathToFileUrl(os.path.abspath(path))
        self.doc = self.desktop.loadComponentFromURL(url, "_blank", 0, (_pv("Hidden", True),))
        if self.doc is None:
            raise RuntimeError("LibreOffice не смог открыть файл")
        self.iterate = bool(iterate or getattr(self.doc, "IsIterationEnabled", False))
        if self.iterate:
            self.doc.IsIterationEnabled = True
            self.doc.IterationCount = 1000
            self.doc.IterationEpsilon = 1e-7
        self.doc.calculateAll()
        return {"iterate": self.iterate}

    def scenario(self, overrides, watch, reads):
        saved = []
        for o in overrides:
            r = self.rng(o["ref"])
            saved.append((r, r.getFormulaArray()))
            vals = tuple(tuple(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                               else (v if v is not None else "") for v in row) for row in o["values"])
            r.setDataArray(vals)
        iters = self.converge(watch)
        values = self.read(reads)
        for r, formulas in saved:
            r.setFormulaArray(formulas)
        self.converge(watch)
        return {"values": values, "iters": iters}


def _handle(w, req):
    cmd = req.get("cmd")
    if cmd == "open":
        return w.open(req["path"], req.get("iterate", False))
    if cmd == "read":
        return {"values": w.read(req["ranges"])}
    if cmd == "scenario":
        return w.scenario(req.get("overrides", []), req.get("watch", []), req.get("reads", []))
    if cmd == "ping":
        return {"pong": True}
    raise ValueError(f"unknown cmd {cmd}")


def serve(*args):
    """Точка входа макроса: vnd.sun.star.script:cf_worker.py$serve?language=Python&location=user"""
    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.environ.get("CF_WORKER_CFG") or os.path.join(here, "cf_worker.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    desktop = XSCRIPTCONTEXT.getDesktop()  # noqa: F821 — подставляется LibreOffice
    w = Worker(desktop)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", int(cfg["port"])))
    srv.listen(1)
    conn, _ = srv.accept()
    f = conn.makefile("rwb")
    hello = json.loads(f.readline().decode("utf-8") or "{}")
    if hello.get("token") != cfg.get("token"):
        conn.close()
        srv.close()
        desktop.terminate()
        return
    f.write((json.dumps({"ok": True, "ready": True}) + "\n").encode("utf-8"))
    f.flush()
    while True:
        line = f.readline()
        if not line:
            break
        try:
            req = json.loads(line.decode("utf-8"))
            if req.get("cmd") == "quit":
                f.write(b'{"ok": true}\n')
                f.flush()
                break
            res = _handle(w, req)
            res["ok"] = True
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        f.write((json.dumps(res, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        f.flush()
    try:
        if w.doc is not None:
            w.doc.close(True)
    except Exception:
        pass
    conn.close()
    srv.close()
    try:
        desktop.terminate()
    except Exception:
        pass


g_exportedScripts = (serve,)
