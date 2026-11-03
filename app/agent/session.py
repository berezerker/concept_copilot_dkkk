"""Сессия: одна загруженная модель, её индекс, движок пересчёта, карточка и история диалога."""
from __future__ import annotations

import os
import shutil
import threading
import uuid

from ..workbook.card import build_card
from ..workbook.engine import make_engine, EngineUnavailable
from ..workbook.index import build_index


class Session:
    def __init__(self, workdir: str):
        self.id = uuid.uuid4().hex[:12]
        self.workdir = os.path.join(workdir, self.id)
        os.makedirs(self.workdir, exist_ok=True)
        self.idx = None
        self.card = None
        self.engine = None
        self.engine_error = None
        self.engine_open = False
        self.forecast_start = None
        self.history = []          # сообщения диалога (формат OpenAI) без system
        self.plan = None
        self.lock = threading.Lock()
        self.base_cache = {}       # ref -> значения базы после сходимости
        self.results = []          # результаты стресс-прогонов (для выгрузки)
        self.sem = None            # смысловой слой: паспорт строк и факторы (workbook/semantics.py)
        self.cache_dir = None
        self.client = None         # LLM текущего хода (для эксперта и заключения)

    def load(self, src_path: str, filename: str, cache_dir: str, engine_kind: str | None, progress=None):
        dst = os.path.join(self.workdir, os.path.basename(filename))
        shutil.copyfile(src_path, dst)                       # работаем только с копией
        self.idx = build_index(dst, cache_dir=cache_dir, progress=progress)
        self.card = build_card(self.idx)
        self.forecast_start = self.card.get("forecast_start")
        from ..workbook import semantics as sm
        self.cache_dir = cache_dir
        if progress:
            progress("Смысловой слой: паспорт строк и факторы…")
        self.sem = sm.load_cached(cache_dir, self.idx.file_hash) if cache_dir else None
        if not self.sem:
            self.sem = sm.build_semantics(self.idx, self.card, self.forecast_start)
            if cache_dir:
                sm.save(cache_dir, self.sem)
        if progress:
            progress("Запускаю движок пересчёта…")
        if self.engine is None:
            from ..logbus import EngineProxy
            self.engine = EngineProxy(make_engine(engine_kind), self.id)
        if self.engine.name == "none":
            self.engine_error = "; ".join(getattr(self.engine, "errors", [])) or "движок не настроен"
        else:
            try:
                self.engine.open(self._engine_copy(src_path, dst), iterate=self.idx.iterate)
                self.engine_open = True
            except Exception as e:  # модель доступна только для чтения
                self.engine_error = str(e)
        self.history, self.plan, self.base_cache, self.results = [], None, {}, []
        return self.card

    def _engine_copy(self, src_path, dst):
        """Рабочая копия для движка по постоянному пути <workspace>/excel/<хэш файла>.xlsx.
        Excel для Mac спрашивает доступ к каждому новому пути — с постоянным путём только при первой загрузке модели;
        копия, сохранённая Excel после первого открытия (с восстановлением), переиспользуется."""
        d = os.path.join(os.path.dirname(self.workdir), "excel")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, self.idx.file_hash + os.path.splitext(dst)[1].lower())
        if not os.path.exists(p):
            shutil.copyfile(dst, p)
        return p

    def close(self):
        if self.engine is not None:
            try:
                self.engine.close()
            except Exception:
                pass

    def require_engine(self):
        if not self.engine_open:
            raise EngineUnavailable(
                "Пересчёт недоступен: " + (self.engine_error or "движок не запущен") +
                ". Чтение и поиск по модели работают, стресс-тесты — нет.")
