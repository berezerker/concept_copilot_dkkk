"""Движки пересчёта модели: Excel через xlwings (основной) и LibreOffice (запасной).

Единый интерфейс:
    open(path, iterate)                         — открыть копию файла
    read(refs) -> [2D-списки значений]          — прочитать диапазоны
    scenario(overrides, watch, reads)           — подставить значения, пересчитать до сходимости,
                                                  прочитать, вернуть модель в исходное состояние
Исходный файл никогда не изменяется: движки работают с копией в рабочей папке сессии
и не сохраняют её.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading

from .transaction import RestorationError, same_values, split_ref, validate_overrides

# Все сессии на Mac используют один процесс Excel. Сценарий и откат не должны
# перемежаться с чтением/записью другой сессии даже при разных worker-потоках.
_EXCEL_LOCK = threading.RLock()


class EngineUnavailable(RuntimeError):
    pass


class CalcEngine:
    name = "none"

    def open(self, path: str, iterate: bool = False):
        raise EngineUnavailable("Движок пересчёта не настроен")

    def read(self, refs):
        raise EngineUnavailable("Движок пересчёта не настроен")

    def scenario(self, overrides, watch, reads):
        raise EngineUnavailable("Движок пересчёта не настроен")

    def close(self):
        pass


# ───────────────────────── LibreOffice ─────────────────────────
def _soffice_path():
    cfg = os.environ.get("CF_SOFFICE")
    if cfg:
        return cfg
    if platform.system() == "Darwin":
        p = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        if os.path.exists(p):
            return p
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    if platform.system() == "Windows":
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")):
            p = os.path.join(base, "LibreOffice", "program", "soffice.exe")
            if os.path.exists(p):
                return p
    return None


_TEMPLATE = {}


def _template_profile(so):
    """Профиль LibreOffice, инициализированный один раз (soffice --terminate_after_init).
    На «пустом» профиле LibreOffice при первом старте перезапускается и теряет аргументы,
    поэтому макрос нужно запускать уже на готовом профиле."""
    import pathlib
    import tempfile
    if so in _TEMPLATE and os.path.isdir(_TEMPLATE[so]):
        return _TEMPLATE[so]
    base = os.environ.get("CF_LO_PROFILE_DIR") or os.path.join(tempfile.gettempdir(), "cf_agent_lo_template")
    marker = os.path.join(base, "user", "registrymodifications.xcu")
    if not os.path.exists(marker):
        os.makedirs(base, exist_ok=True)
        subprocess.run([so, "--headless", "--invisible", "--norestore", "--terminate_after_init",
                        "-env:UserInstallation=" + pathlib.Path(base).as_uri()],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if not os.path.exists(marker):
            raise EngineUnavailable("Не удалось инициализировать профиль LibreOffice. Откройте LibreOffice один раз "
                                    "вручную (на Mac — разрешите запуск в «Конфиденциальность и безопасность») и повторите.")
    _TEMPLATE[so] = base
    return base


class LibreOfficeEngine(CalcEngine):
    """Пересчёт в LibreOffice. Воркер — Python-макрос (lo_macro.py), выполняемый внутри soffice;
    связь с ним — по локальному сокету. Встроенный Python LibreOffice напрямую не запускаем
    (на macOS это запрещено политикой запуска)."""
    name = "libreoffice"

    def __init__(self, start_timeout: float = 120.0):
        import json as _json
        import pathlib
        import secrets
        import socket
        import tempfile
        import time

        so = _soffice_path()
        if not so:
            raise EngineUnavailable("LibreOffice не найден. Установите его (на Mac: brew install --cask libreoffice) "
                                    "или укажите путь к soffice в CF_SOFFICE")
        self.lock = threading.Lock()
        self.profile = tempfile.mkdtemp(prefix="cf_lo_profile_")
        shutil.rmtree(self.profile)
        shutil.copytree(_template_profile(so), self.profile)    # готовый (инициализированный) профиль
        scripts = os.path.join(self.profile, "user", "Scripts", "python")
        os.makedirs(scripts, exist_ok=True)
        shutil.copyfile(os.path.join(os.path.dirname(__file__), "lo_macro.py"), os.path.join(scripts, "cf_worker.py"))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        token = secrets.token_hex(16)
        cfg = os.path.join(scripts, "cf_worker.json")
        with open(cfg, "w", encoding="utf-8") as f:
            _json.dump({"port": port, "token": token}, f)
        env = dict(os.environ, CF_WORKER_CFG=cfg)
        self.proc = subprocess.Popen(
            [so, "--headless", "--invisible", "--nologo", "--norestore", "--nolockcheck",
             "-env:UserInstallation=" + pathlib.Path(self.profile).as_uri(),
             "vnd.sun.star.script:cf_worker.py$serve?language=Python&location=user"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        t0, last = time.time(), None
        self.sock = None
        while time.time() - t0 < start_timeout:
            if self.proc.poll() is not None and time.time() - t0 > 20:
                raise EngineUnavailable(f"LibreOffice завершился при запуске (код {self.proc.returncode}). "
                                        "Проверьте, что LibreOffice открывается вручную, и что не запущен другой его экземпляр с тем же профилем.")
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError as e:
                last = e
                time.sleep(0.5)
        if self.sock is None:
            self.proc.kill()
            raise EngineUnavailable(f"Воркер LibreOffice не ответил за {start_timeout:.0f} с: {last}")
        self.sock.settimeout(None)
        self.f = self.sock.makefile("rwb")
        self._send({"token": token})
        hello = self._recv()
        if not hello.get("ok"):
            raise EngineUnavailable(f"Воркер LibreOffice не стартовал: {hello}")

    def _send(self, obj):
        import json as _json
        self.f.write((_json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.f.flush()

    def _recv(self):
        import json as _json
        line = self.f.readline()
        if not line:
            raise EngineUnavailable("Воркер LibreOffice завершился")
        return _json.loads(line.decode("utf-8"))

    def _call(self, **req):
        with self.lock:
            self._send(req)
            res = self._recv()
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "ошибка воркера"))
        return res

    def open(self, path, iterate=False):
        return self._call(cmd="open", path=os.path.abspath(path), iterate=iterate)

    def read(self, refs):
        return self._call(cmd="read", ranges=list(refs))["values"]

    def scenario(self, overrides, watch, reads):
        res = self._call(cmd="scenario", overrides=overrides, watch=watch, reads=reads)
        return res["values"], res.get("iters")

    def close(self):
        try:
            self._call(cmd="quit")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


# ───────────────────────── Microsoft Excel (xlwings) — основной ─────────────────────────
def _mac_excel_app():
    """Обычный экземпляр Excel для Mac (xw.App() на Mac создаёт «новый экземпляр», в котором
    открытие файлов часто падает с -50). Если Excel уже открыт — подключаемся к нему."""
    import time
    import xlwings as xw
    was_running = bool(xw.apps.count)
    if not was_running:
        subprocess.run(["open", "-g", "-a", "Microsoft Excel"], check=False)
        t0 = time.time()
        while not xw.apps.count and time.time() - t0 < 60:
            time.sleep(1)
        if not xw.apps.count:
            raise EngineUnavailable("Excel не запустился за 60 с")
        time.sleep(2)
    app = xw.apps.active
    try:
        app._cf_keep_running = was_running
    except Exception:
        pass
    return app



def _as_lit(v, formula=False):
    """Значение → литерал AppleScript."""
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            return '""'
        if isinstance(v, int) or float(v).is_integer() and abs(v) < 1e15:
            return str(int(v))
        return f"{float(v):.16E}"                       # 17 значащих цифр для round-trip double
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _applescript_batch(book, ops):
    """Все записи одним скриптом osascript: на Mac каждая запись через xlwings — несколько Apple-событий с ответом
    в Python (~0,5 с на блок); один скрипт с десятками «set value» в разы быстрее."""
    bq = book.replace('"', '\\"')
    lines = ['tell application "Microsoft Excel"', f'tell workbook "{bq}"']
    for sheet, addr, kind, vals in ops:
        sq = sheet.replace('"', '\\"')
        rows = ", ".join("{" + ", ".join(_as_lit(x) for x in r) + "}" for r in vals)
        prop = "formula" if kind == "formula" else "value"
        lines.append(f'set {prop} of range "{addr}" of worksheet "{sq}" to {{{rows}}}')
    lines += ["end tell", "end tell"]
    r = subprocess.run(["osascript", "-"], input="\n".join(lines), capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"osascript: {r.stderr.strip()[:300]}")


class ExcelEngine(CalcEngine):
    """Пересчёт в установленном Excel через xlwings (macOS и Windows). Самая точная совместимость
    с формулами модели. Все вызовы Excel идут из одного выделенного потока (требование COM на
    Windows; на macOS так же надёжнее для AppleScript), остальные потоки ставят задачи в очередь."""
    name = "excel"

    def __init__(self):
        import queue
        try:
            import xlwings  # noqa: F401
        except Exception as e:
            raise EngineUnavailable(f"xlwings не установлен ({e}). Выполните: pip install -r requirements.txt")
        self.q = queue.Queue()
        self.app = None
        self.wb = None
        self.iterate = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self._submit(self._start_app)          # проверяем, что Excel реально запускается

    # ── выделенный поток ──
    def _loop(self):
        try:
            import pythoncom  # только Windows
            pythoncom.CoInitialize()
        except Exception:
            pass
        while True:
            fn, args, done = self.q.get()
            if fn is None:
                break
            try:
                with _EXCEL_LOCK:
                    done["result"] = fn(*args)
            except Exception as e:
                done["error"] = e
            done["event"].set()

    def _submit(self, fn, *args):
        done = {"event": threading.Event()}
        self.q.put((fn, args, done))
        done["event"].wait()
        if "error" in done:
            err = done["error"]
            if fn is self._start_app:
                raise EngineUnavailable(f"Excel не запустился: {err}")
            raise err
        return done.get("result")

    def _start_app(self):
        import xlwings as xw
        if platform.system() == "Darwin":
            self.app = _mac_excel_app()
            try:
                self._books_before = len(self.app.books)
            except Exception:
                self._books_before = 1
        else:
            self.app = xw.App(visible=False, add_book=False)
            self._books_before = 0
            self._quiet_app()
        # на Mac режимы (перерисовка, ручной пересчёт) ставим только ПОСЛЕ открытия книги:
        # Excel для Mac без открытой книги отвечает на open_workbook ошибкой -50 (Parameter error)

    def _quiet_app(self):
        """Без перерисовки экрана и диалогов; ручной пересчёт (иначе Excel пересчитывает модель на каждую запись)."""
        for attr, val in (("display_alerts", False), ("screen_updating", False)):
            try:
                setattr(self.app, attr, val)
            except Exception:
                pass
        if not hasattr(self, "_calc_mode_before"):
            try:
                self._calc_mode_before = self.app.calculation
            except Exception:
                self._calc_mode_before = None
        try:
            self.app.calculation = "manual"
        except Exception:
            pass

    def _hide(self):
        """Скрыть Excel, если в нём нет других книг пользователя (CF_EXCEL_VISIBLE=1 — не скрывать)."""
        if os.environ.get("CF_EXCEL_VISIBLE", "0") == "1" or getattr(self, "_books_before", 1) > 0:
            return
        try:
            self.app.visible = False
        except Exception:
            pass

    def _set_iteration(self):
        """Итеративный расчёт для моделей с циклическими ссылками. Ветка по платформе: на Mac присваивание
        api.Iteration = True не падает (просто атрибут Python-объекта), но ничего не включает — раньше из-за этого
        на Mac итерации не включались и всё, что зависит от цикла (долг, проценты, DSCR, Net Debt/EBITDA), не пересчитывалось."""
        api = self.app.api
        if platform.system() != "Darwin":
            try:
                api.Iteration = True
                api.MaxIterations = 1000
                api.MaxChange = 1e-7
                self.iteration_on = bool(api.Iteration)
            except Exception as e:
                self.iteration_error = f"{type(e).__name__}: {e}"
                self.iteration_on = None
            return
        try:                                   # macOS (appscript)
            api.iteration.set(True)
            api.max_iterations.set(1000)
            api.max_change.set(1e-7)
        except Exception as e:
            self.iteration_error = f"{type(e).__name__}: {e}"
        try:                                   # проверка: без итераций Excel не считает циклы и всё, что от них зависит
            self.iteration_on = bool(api.iteration.get())
        except Exception:
            self.iteration_on = None

    def _rng(self, ref):
        sheet, addr = split_ref(ref)
        return self.wb.sheets[sheet].range(addr)

    def _read(self, refs):
        if getattr(self, "_tainted", False):
            raise RestorationError("Восстановление книги не подтверждено. Загрузите модель заново.")
        if getattr(self, "_dirty", None):          # после сценария формулы восстановлены, но ещё не пересчитаны
            watch, self._dirty = self._dirty, None
            self._converge(watch)
        return self._read_raw(refs)

    def _read_raw(self, refs):
        """Строки одного листа с одинаковыми столбцами читаем одним диапазоном-рамкой и режем
        (на Mac каждое чтение — Apple-событие; 300 строк фактора = 300 событий → единицы)."""
        import re as _re
        rx = _re.compile(r"^(.*)!\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)$")
        out = [None] * len(refs)
        groups = {}
        for i, r in enumerate(refs):
            m = rx.match(r)
            if m and m.group(3) == m.group(5):
                groups.setdefault((m.group(1), m.group(2), m.group(4)), []).append((int(m.group(3)), i))
            else:
                v = self._rng(r).options(ndim=2).value
                out[i] = [[x for x in row] for row in v]
        for (sheet, c1, c2), items in groups.items():
            rows = sorted(r for r, _ in items)
            chunks, cur = [], [rows[0]]
            for r in rows[1:]:                         # рамка не больше 400 строк
                if r - cur[0] > 400:
                    chunks.append(cur)
                    cur = [r]
                else:
                    cur.append(r)
            chunks.append(cur)
            for ch in chunks:
                a, b = ch[0], ch[-1]
                box = self._rng(f"{sheet}!{c1}{a}:{c2}{b}").options(ndim=2).value
                for r, i in items:
                    if a <= r <= b:
                        out[i] = [list(box[r - a])]
        return out

    def _calc(self):
        mode = os.environ.get("CF_CALC_MODE", "calc")      # calc | full (CalculateFull — дольше, но пересчитывает всё)
        if mode == "full":
            try:
                self.app.api.calculate_full()          # macOS appscript
                return
            except Exception:
                try:
                    self.app.api.CalculateFull()       # Windows COM
                    return
                except Exception:
                    pass
        try:
            self.app.calculate()
        except Exception:
            self.wb.app.calculate()

    def _converge(self, watch, max_iter=40, tol=1e-6):
        import time
        prev = None
        self.calc_times = []
        for k in range(1, max_iter + 1):
            t = time.time()
            self._calc()
            self.calc_times.append(round(time.time() - t, 2))
            cur = [v for b in self._read_raw(watch) for row in b for v in row
                   if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if prev is not None and len(cur) == len(prev) and all(
                    abs(a - b) <= tol * max(1.0, abs(b)) for a, b in zip(cur, prev)):
                return k
            prev = cur
            if not self.iterate and k >= int(os.environ.get("CF_CALC_PASSES", "1")):
                return k
        raise RuntimeError(f"Excel не достиг сходимости за {max_iter} итераций")

    def _open_mac(self, path):
        """Excel для Mac. Важно: на время открытия диалоги ВКЛЮЧЕНЫ. Excel в песочнице спрашивает доступ к файлу
        («Предоставить доступ»), а для обезличенных моделей предлагает восстановить содержимое («Да»).
        С выключенными диалогами Excel не показывает их и отвечает ошибкой -50 (Parameter error).
        Путь рабочей копии постоянный для модели, поэтому спрашивает только при первом открытии."""
        import time
        for attr, val in (("display_alerts", True), ("screen_updating", True)):
            try:
                setattr(self.app, attr, val)
            except Exception:
                pass
        try:
            self.app.activate(steal_focus=True)        # чтобы диалог доступа/восстановления был виден
        except Exception:
            pass
        errors = []
        name = os.path.basename(path)
        expected = os.path.realpath(path)

        def matches(book):
            return book.name == name and os.path.realpath(book.fullname) == expected

        def wait_book(nm, timeout=180):
            t0 = time.time()
            while time.time() - t0 < timeout:
                for b in self.app.books:
                    if matches(b):
                        return b
                time.sleep(0.5)
            raise RuntimeError(f"книга {nm} не появилась в Excel за {timeout} с (не нажат запрос доступа?)")

        for b in self.app.books:                        # уже открыта (повторная загрузка / переподключение)
            if matches(b):
                return b
            if b.name == name:
                raise RuntimeError("В Excel открыта одноимённая книга по другому пути. "
                                   "Для расчёта нужна рабочая копия текущей загрузки.")
        # Сначала через LaunchServices (как двойной клик в Finder): Excel получает доступ к файлу от системы
        # и НЕ показывает «Предоставить доступ». AppleScript-открытие (books.open) — только запасной путь.
        try:
            subprocess.run(["open", "-g", "-a", "Microsoft Excel", path], check=False)
            return wait_book(name, timeout=int(os.environ.get("CF_EXCEL_OPEN_TIMEOUT", "180")))
        except Exception as e:
            errors.append(f"open -a: {type(e).__name__}: {e}")
        try:
            book = self.app.books.open(path)
            if not matches(book):
                raise RuntimeError("Открытая книга Excel не совпадает с рабочей копией")
            return book
        except Exception as e:
            errors.append(f"xlwings open: {type(e).__name__}: {e}")
        raise RuntimeError("Excel не смог открыть файл. Если в Excel был запрос доступа или восстановления — "
                           "нажмите «Предоставить доступ» / «Да» и загрузите модель ещё раз. " +
                           " | ".join(str(x)[:300] for x in errors))

    def _open(self, path, iterate):
        if self.wb is not None:
            try:
                self.wb.close()
            except Exception:
                pass
        path = os.path.abspath(path)
        self._path = path
        if platform.system() == "Darwin":
            self.wb = self._open_mac(path)
            marker = path + ".excel_ok"
            if not os.path.exists(marker):          # первая загрузка: сохраняем копию так, как её открыл Excel
                try:                                # (после восстановления) — дальше без запросов
                    self.wb.save(path)
                    open(marker, "w").close()
                except Exception:
                    pass
        else:
            self.wb = self.app.books.open(path, update_links=False)
        self.iterate = iterate
        if iterate:
            self._set_iteration()
        self._quiet_app()
        self._hide()
        self._calc()
        self._tainted = False
        self._formula_cache, self._box_cache, self._dirty = {}, {}, None
        return {"iterate": iterate}

    def _coalesce(self, overrides):
        """Excel для Mac: каждая запись диапазона — отдельное AppleScript-событие (~0,5 с). Строки шока одного листа
        с одинаковыми столбцами объединяем в крупные блоки, «перешагивая» строки между ними, если в них нет формул
        (их исходные значения пишем как есть). Строки с формулами между шоками разрывают блок."""
        import re as _re
        rx = _re.compile(r"^(.*)!([A-Z]{1,3})(\d+):([A-Z]{1,3})(\d+)$")
        groups, other = {}, []
        for o in overrides:
            m = rx.match(o["ref"])
            if not m:
                other.append(o)
                continue
            groups.setdefault((m.group(1), m.group(2), m.group(4)), []).append((int(m.group(3)), int(m.group(5)), o["values"]))
        out = list(other)
        for (sheet, c1, c2), items in groups.items():
            items.sort()
            r1, r2 = items[0][0], max(b for _, b, _ in items)
            if r2 - r1 > 400 or len(items) == 1:
                out += [{"ref": f"{sheet}!{c1}{a}:{c2}{b}", "values": v} for a, b, v in items]
                continue
            box = f"{sheet}!{c1}{r1}:{c2}{r2}"
            if box not in self._box_cache:
                rng = self._rng(box)
                self._box_cache[box] = (rng.options(ndim=2).formula, rng.options(ndim=2).value)
            bf, bv = self._box_cache[box]
            shocked = {}
            for a, b, v in items:
                for i, r in enumerate(range(a, b + 1)):
                    shocked[r] = v[i]
            segs, cur = [], None
            for r in range(r1, r2 + 1):
                has_f = any(isinstance(x, str) and x.startswith("=") for x in bf[r - r1])
                if r in shocked or not has_f:
                    if cur is None:
                        cur = {"start": r, "rows": [], "shocked": []}
                    cur["rows"].append(shocked.get(r, bv[r - r1]))
                    if r in shocked:
                        cur["shocked"].append(r)
                    continue
                if cur:
                    segs.append(cur)
                cur = None
            if cur:
                segs.append(cur)
            for sg in segs:
                if not sg["shocked"]:
                    continue
                a, b = min(sg["shocked"]), max(sg["shocked"])     # без «пустых» краёв
                rows = sg["rows"][a - sg["start"]: b - sg["start"] + 1]
                out.append({"ref": f"{sheet}!{c1}{a}:{c2}{b}", "values": rows})
        return out

    def _scenario(self, overrides, watch, reads, expected_watch=None):
        import time
        self.last_transaction = {"status": "preflight", "restored": False}
        if getattr(self, "_tainted", False):
            raise RestorationError("Восстановление книги не подтверждено. Загрузите модель заново.")
        preflight = validate_overrides(overrides)
        # Свежий снимок: Excel может быть изменён пользователем между сценариями.
        self._box_cache = {}
        if platform.system() == "Darwin" and all("expected_input" not in o for o in overrides):
            merged = self._coalesce(overrides)
            try:
                validate_overrides(merged)
            except ValueError:
                pass  # расширенные блоки могут перекрыть другие полосы; исходные уже проверены
            else:
                overrides = merged
        watch = list(dict.fromkeys(list(watch) + list(reads)))
        self._converge(watch)
        baseline = self._read_raw(watch)
        if expected_watch is not None:
            for ref, value in zip(watch, baseline):
                if ref in expected_watch and not same_values(value, expected_watch[ref]):
                    raise ValueError(f"{ref}: база изменилась после предпроверки. Подготовьте сценарий заново.")
        saved = []
        self._dirty = None
        T = {"save": 0.0, "write": 0.0, "calc": 0.0, "read": 0.0, "restore": 0.0}
        batch = platform.system() == "Darwin" and os.environ.get("CF_MAC_BATCH", "1") == "1" and overrides \
            and not getattr(self, "_batch_broken", False)
        t = time.time()
        for o in overrides:
            r = self._rng(o["ref"])
            f = r.options(ndim=2).formula
            pure = all(not (isinstance(x, str) and x.startswith("=")) for row in f for x in row)
            original = r.options(ndim=2).value if pure else None
            if "expected_input" in o and (not pure or not same_values(original, o["expected_input"], tol=0)):
                raise ValueError(f"{o['ref']}: допущение изменилось или стало формулой. Проверьте ячейку заново.")
            saved.append((o["ref"], r, f, original))
        # Даты и другие типы передаём через нативный bridge, а не превращаем в строки AppleScript.
        if batch:
            blocks = [o["values"] for o in overrides] + [orig if orig is not None else f for _, _, f, orig in saved]
            batch = all(v is None or isinstance(v, (str, bool, int, float)) for b in blocks for row in b for v in row)
        T["save"] = time.time() - t
        # Все адреса чтения и записи проверены, все снимки сняты ДО первой записи.
        self.last_transaction = {"status": "running", "preflight": preflight, "restored": False}
        try:
            t = time.time()
            if batch:
                _applescript_batch(self.wb.name, [(*self._split(o["ref"]), "value", o["values"]) for o in overrides])
                for o, (_, r, _, _) in zip(overrides, saved):
                    self._ensure_written(o["ref"], r, "value", o["values"], block_written=True)
            else:
                for o, (_, r, _, _) in zip(overrides, saved):
                    self._ensure_written(o["ref"], r, "value", o["values"])
            T["write"] = time.time() - t
            t = time.time()
            iters = self._converge(watch)
            T["calc"] += time.time() - t
            t = time.time()
            values = self._read_raw(reads)
            # Ошибка Excel часто возвращается как None: не выдаём её за числовой результат.
            import math
            for ref, block in zip(reads, values):
                before = baseline[watch.index(ref)]
                for old_row, new_row in zip(before, block):
                    for old, new in zip(old_row, new_row):
                        if isinstance(old, (float, int)) and not isinstance(old, bool) and (
                                not isinstance(new, (float, int)) or isinstance(new, bool) or not math.isfinite(new)):
                            raise ValueError(f"{ref}: вместо числовой метрики получена ошибка/пустое значение")
            T["read"] += time.time() - t
            self.last_transaction["status"] = "calculated"
        except BaseException:
            self.last_transaction["status"] = "failed"
            raise
        finally:
            t = time.time()
            try:
                self._restore_transaction(saved, watch, baseline, batch)
                self.last_transaction["restored"] = True
            except Exception as e:
                self._tainted = True
                self.last_transaction.update(status="restore_failed", error=str(e))
                raise RestorationError("Не удалось подтвердить восстановление Excel; дальнейшие расчёты заблокированы. " + str(e)) from e
            finally:
                T["restore"] = time.time() - t
                self.last_timing = {k: round(v, 2) for k, v in T.items()}
                self._box_cache, self._formula_cache = {}, {}
                self._dirty = None
        self.last_transaction["status"] = "verified"
        self.last_timing = {k: round(v, 2) for k, v in T.items()}
        return values, iters

    def _restore_transaction(self, saved, watch, baseline, batch):
        restored_batch = False
        if batch and saved:
            try:
                _applescript_batch(self.wb.name, [(*self._split(ref), "value" if orig is not None else "formula",
                                                orig if orig is not None else f) for ref, _, f, orig in saved])
                restored_batch = True
            except Exception:
                self._batch_broken = True
        errors = []
        restored_refs = set()
        if not restored_batch:
            for ref, r, f, orig in saved:
                try:
                    if orig is not None:
                        r.value = orig
                    else:
                        r.formula = f
                    restored_refs.add(ref)
                except Exception:
                    pass  # точный повтор и итоговая ошибка — ниже; остальные блоки всё равно восстанавливаем
        # Проверяем каждый диапазон, даже если восстановление другого не удалось.
        for ref, r, f, orig in saved:
            try:
                self._ensure_written(ref, r, "value" if orig is not None else "formula",
                                     orig if orig is not None else f,
                                     block_written=restored_batch or ref in restored_refs)
            except Exception as e:
                errors.append(f"{ref}: {e}")
        if errors:
            raise RestorationError("; ".join(errors)[:1000])
        self._converge(watch)
        if not same_values(self._read_raw(watch), baseline):
            raise RestorationError("Контрольные показатели после отката отличаются от базы")

    def _ensure_written(self, ref, rng, kind, expected, block_written=False):
        """Записать матрицу и подтвердить точное значение.

        Excel for Mac иногда принимает многорядную запись без ошибки, но раскладывает
        элементы по диапазону неверно. После обычной/пакетной записи проверяем диапазон,
        затем деградируем до строк и, если нужно, до отдельных ячеек.
        """
        from openpyxl.utils.cell import get_column_letter, range_boundaries

        prop = "value" if kind == "value" else "formula"

        def read(target):
            return getattr(target.options(ndim=2), prop)

        def write(target, values):
            setattr(target, prop, values)

        if not block_written:
            write(rng, expected)
        if same_values(read(rng), expected, tol=0):
            return

        sheet, addr = self._split(ref)
        c1, r1, c2, r2 = range_boundaries(addr)
        for offset, row in enumerate(expected):
            row_ref = f"'{sheet.replace(chr(39), chr(39) * 2)}'!{get_column_letter(c1)}{r1 + offset}:{get_column_letter(c2)}{r1 + offset}"
            write(self._rng(row_ref), [row])
        if same_values(read(rng), expected, tol=0):
            return

        for i, row in enumerate(expected):
            for j, value in enumerate(row):
                cell_ref = f"'{sheet.replace(chr(39), chr(39) * 2)}'!{get_column_letter(c1 + j)}{r1 + i}"
                write(self._rng(cell_ref), [[value]])
        if not same_values(read(rng), expected, tol=0):
            raise RuntimeError(f"{ref}: значения/формулы не записаны точно")

    def _split(self, ref):
        return split_ref(ref)

    def _close(self):
        try:
            if getattr(self, "_calc_mode_before", None):
                self.app.calculation = self._calc_mode_before
            self.app.screen_updating = True
            self.app.display_alerts = True
        except Exception:
            pass
        if self.wb is not None:
            self.wb.close()                    # без сохранения: копия модели остаётся нетронутой
        if self.app is not None and not getattr(self.app, "_cf_keep_running", False):
            self.app.quit()
        p = getattr(self, "_mac_copy", None)
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    # ── публичный интерфейс ──
    def open(self, path, iterate=False):
        return self._submit(self._open, path, iterate)

    def _lost(self, e):
        s = str(e)
        return "-1728" in s or "does not exist" in s or "-1719" in s or "-609" in s

    def _reconnect(self):
        """Книгу закрыли (вручную или Excel перезапустился): переоткрываем ту же рабочую копию без запросов."""
        import xlwings as xw
        if platform.system() == "Darwin":
            self.app = _mac_excel_app() if not xw.apps.count else xw.apps.active
        self.wb = None
        self._formula_cache, self._box_cache, self._dirty = {}, {}, None
        return self._open(self._path, self.iterate)

    def _retry(self, fn, *args):
        try:
            return self._submit(fn, *args)
        except Exception as e:
            if isinstance(e, RestorationError):
                raise
            if not getattr(self, "_path", None) or not self._lost(e):
                raise
            self._submit(self._reconnect)
            return self._submit(fn, *args)

    def read(self, refs):
        return self._retry(self._read, list(refs))

    def scenario(self, overrides, watch, reads, *, expected_watch=None):
        return self._retry(self._scenario, overrides, watch, reads, expected_watch)

    def close(self):
        try:
            self._submit(self._close)
        except Exception:
            pass
        self.q.put((None, None, None))


def make_engine(kind: str | None = None) -> CalcEngine:
    """CF_ENGINE: auto (по умолчанию: Excel, при неудаче — LibreOffice) | excel | libreoffice | none."""
    kind = (kind or os.environ.get("CF_ENGINE", "auto")).lower()
    order = {"auto": ["excel", "libreoffice"], "excel": ["excel"], "libreoffice": ["libreoffice"],
             "none": []}.get(kind, ["excel", "libreoffice"])
    errors = []
    for k in order:
        try:
            return ExcelEngine() if k == "excel" else LibreOfficeEngine()
        except Exception as e:
            errors.append(f"{'Excel' if k == 'excel' else 'LibreOffice'}: {e}")
    eng = CalcEngine()
    eng.errors = errors or ["пересчёт отключён (CF_ENGINE=none)"]
    return eng
