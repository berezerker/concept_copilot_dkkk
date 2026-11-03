"""Журнал расчётов: кольцевой буфер событий для панели «Журнал» + состояние «что сейчас считается».

Пишут сюда обёртки движка (каждый пересчёт и чтение — с размером и временем), обёртка LLM (каждый вызов —
модель, время, токены), SSE-задачи (инструменты, прогресс, ошибки). Панель в браузере читает поток событий
и раз в 2 секунды получает «пульс» с текущей операцией и временем без новых событий — видно, работает или висит.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class Cancelled(BaseException):
    """Пользователь нажал «Остановить»."""


class LogBus:
    def __init__(self, size=3000):
        self.buf = deque(maxlen=size)
        self.seq = 0
        self.cv = threading.Condition()
        self.ops = {}                    # id задачи -> {name, started, last, step}
        self._op_seq = 0
        self.cancel_flags = set()        # session_id, для которых запрошена остановка

    # ── события ──
    def log(self, text, src="app", level="info", sid=None):
        with self.cv:
            self.seq += 1
            self.buf.append({"seq": self.seq, "t": time.time(), "src": src, "level": level, "text": str(text)[:600], "sid": sid})
            for op in self.ops.values():
                op["last"] = time.time()
            self.cv.notify_all()

    def since(self, seq, timeout=2.0):
        with self.cv:
            if self.seq <= seq:
                self.cv.wait(timeout)
            return [x for x in self.buf if x["seq"] > seq]

    # ── текущие операции ──
    def begin(self, name, sid=None):
        with self.cv:
            self._op_seq += 1
            oid = self._op_seq
            self.ops[oid] = {"name": name, "started": time.time(), "last": time.time(), "step": "", "sid": sid}
        self.log(f"▶ {name}", "op", sid=sid)
        return oid

    def step(self, oid, text):
        op = self.ops.get(oid)
        if op:
            op["step"] = text
            op["last"] = time.time()

    def end(self, oid, ok=True, note=""):
        op = self.ops.pop(oid, None)
        if op:
            dt = time.time() - op["started"]
            self.log(f"{'■' if ok else '✗'} {op['name']} — {dt:.1f} с{(' · ' + note) if note else ''}", "op",
                     "info" if ok else "error", op.get("sid"))

    def status(self):
        now = time.time()
        return [{"name": o["name"], "elapsed": round(now - o["started"], 1), "idle": round(now - o["last"], 1),
                 "step": o["step"]} for o in list(self.ops.values())]

    # ── остановка ──
    def request_cancel(self, sid):
        self.cancel_flags.add(sid)
        self.log("Запрошена остановка текущего расчёта", "op", "warn", sid)

    def clear_cancel(self, sid):
        self.cancel_flags.discard(sid)

    def check(self, sid):
        if sid in self.cancel_flags:
            raise Cancelled("Остановлено пользователем")


BUS = LogBus()


class EngineProxy:
    """Обёртка движка пересчёта: журнал каждой операции (размер, время, итерации) и точка остановки."""

    def __init__(self, engine, sid):
        self._e = engine
        self._sid = sid
        self.name = engine.name
        self.n_scen = 0

    def __getattr__(self, k):
        return getattr(self._e, k)

    def read(self, refs):
        BUS.check(self._sid)
        t0 = time.time()
        res = self._e.read(refs)
        dt = time.time() - t0
        if dt > 0.3 or len(refs) > 20:
            BUS.log(f"чтение {len(refs)} диапазонов — {dt:.2f} с", "engine", sid=self._sid)
        return res

    def scenario(self, overrides, watch, reads):
        BUS.check(self._sid)
        self.n_scen += 1
        cells = sum(len(o["values"]) * (len(o["values"][0]) if o["values"] else 0) for o in overrides)
        t0 = time.time()
        res, iters = self._e.scenario(overrides, watch, reads)
        dt = time.time() - t0
        tm = getattr(self._e, "last_timing", None)
        ct = getattr(self._e, "calc_times", None)
        extra = (f" [запись {tm['save'] + tm['write']:.1f} с, пересчёт {tm['calc']:.1f} с ({'+'.join(map(str, ct or []))}), "
                 f"чтение {tm['read']:.1f} с, откат {tm['restore']:.1f} с]") if tm else ""
        BUS.log(f"пересчёт №{self.n_scen}: {len(overrides)} блоков / {cells} ячеек → итераций {iters}, "
                f"прочитано {len(reads)} диапазонов — {dt:.2f} с{extra}", "engine", "warn" if dt > 15 else "info", self._sid)
        return res, iters

    def open(self, path, iterate=False):
        t0 = time.time()
        BUS.log(f"открываю модель в {self._e.name}…", "engine", sid=self._sid)
        r = self._e.open(path, iterate=iterate)
        it = getattr(self._e, "iteration_on", None)
        BUS.log(f"модель открыта в {self._e.name} — {time.time() - t0:.1f} с (итеративный расчёт: {'да' if iterate else 'нет'}"
                f"{'' if not iterate else '; в Excel включён: ' + {True: 'да', False: 'НЕТ — циклы не пересчитаются', None: 'не удалось проверить'}[it]})",
                "engine", "warn" if iterate and it is False else "info", sid=self._sid)
        return r


class ClientProxy:
    """Обёртка LLM-коннектора: журнал каждого вызова."""

    def __init__(self, client, sid=None):
        self._c = client
        self._sid = sid

    def __getattr__(self, k):
        return getattr(self._c, k)

    def chat(self, messages, tools, tool_choice="auto"):
        BUS.check(self._sid)
        t0 = time.time()
        forced = tool_choice.get("name") if isinstance(tool_choice, dict) else None
        BUS.log(f"LLM {self._c.describe()}: запрос ({len(messages)} сообщений, {len(tools)} инструментов"
                f"{', принудительно ' + forced if forced else ''})…", "llm", sid=self._sid)
        for attempt in range(4):                 # сетевые сбои (DNS, обрыв, 429/5xx) — повтор с паузой
            try:
                r = self._c.chat(messages, tools, tool_choice)
                break
            except Exception as e:
                msg = str(e)
                transient = any(x in msg for x in ("Сеть", "timed out", "Timeout", "429", "502", "503", "504", "Connection"))
                BUS.log(f"LLM ошибка через {time.time() - t0:.1f} с: {e}{' — повтор' if transient and attempt < 3 else ''}",
                        "llm", "error", self._sid)
                if not transient or attempt == 3:
                    raise
                for _ in range((5, 15, 40)[attempt]):
                    BUS.check(self._sid)
                    time.sleep(1)
        u = r.usage or {}
        tok = u.get("total_tokens") or ((u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0)) or None
        what = ", ".join(c.name for c in r.tool_calls) or "ответ"
        BUS.log(f"LLM ответ за {time.time() - t0:.1f} с → {what}{f' · {tok} токенов' if tok else ''}", "llm", sid=self._sid)
        return r


class wait_lock:
    """Захват блокировки сессии: если занята другим расчётом — сообщаем, что ждём, а не молча висим."""

    def __init__(self, lock, emit=None, sid=None):
        self.lock, self.emit, self.sid = lock, emit, sid

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            busy = ", ".join(o["name"] for o in BUS.status()) or "другой расчёт"
            msg = f"Ожидает окончания: {busy}"
            BUS.log(msg, "op", "warn", self.sid)
            if self.emit:
                self.emit("status", {"text": msg})
            while not self.lock.acquire(timeout=1.0):
                BUS.check(self.sid)
        return self

    def __exit__(self, *a):
        self.lock.release()
