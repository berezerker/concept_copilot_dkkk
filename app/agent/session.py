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
        self.catalog = None        # справочник модели: показатели, где считаются, как шокируются (workbook/catalog.py)
        self.manifest = None       # единый паспорт модели для агента, UI и preflight
        self.cache_dir = None
        self.client = None         # LLM текущего хода (для эксперта и заключения)

    def load(self, src_path: str, filename: str, cache_dir: str, engine_kind: str | None, progress=None):
        dst = os.path.join(self.workdir, os.path.basename(filename))
        shutil.copyfile(src_path, dst)                       # работаем только с копией
        self.idx = build_index(dst, cache_dir=cache_dir, progress=progress)
        from ..workbook import catalog as cg, semantics as sm
        from ..workbook.structure import guess_forecast_start
        self.cache_dir = cache_dir
        self.forecast_start = guess_forecast_start(self.idx)
        if progress:
            progress("Справочник модели: показатели и где они считаются…")
        self.catalog = cg.load_cached(cache_dir, self.idx.file_hash)
        if self.catalog:
            cg.apply_alt_sets(self.idx, self.catalog)        # выбранные наборы сценариев уже проверены пересчётом
        else:
            self.catalog = cg.build_catalog(self.idx, self.forecast_start, cg.load_pins(cache_dir, self.idx.file_hash))
        self.card = build_card(self.idx, self.catalog)
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
        if self.engine_open and any(not a.get("detected") for a in self.catalog["alt_sets"]):
            try:                                             # какой набор допущений выбран — проверяем пересчётом, один раз на модель
                if cg.probe_alt_sets(self, (lambda k, d: progress(d.get("text", ""))) if progress else None):
                    self.rebuild_catalog()
                elif cache_dir:
                    cg.save(cache_dir, self.catalog)
            except Exception as e:                           # проверка не удалась — остаётся предположение, о нём сказано в справочнике
                self.catalog["warnings"].append(f"Не удалось проверить пересчётом выбранный набор допущений: {str(e)[:120]}")
            self.base_cache = {}
        elif cache_dir:
            cg.save(cache_dir, self.catalog)
        self.refresh_manifest()
        return self.card

    def rebuild_catalog(self):
        """Пересобрать справочник, карточку и смысловой слой (после смены выбранного набора допущений)."""
        from ..workbook import catalog as cg, semantics as sm
        alt = self.catalog["alt_sets"]
        cat = cg.build_catalog(self.idx, self.forecast_start, self.catalog.get("pins"))
        cat["alt_sets"] = alt
        self.catalog = cat
        self.card = build_card(self.idx, cat)
        self.sem = sm.build_semantics(self.idx, self.card, self.forecast_start)
        if self.cache_dir:
            cg.save(self.cache_dir, cat)
            sm.save(self.cache_dir, self.sem)
        self.refresh_manifest()

    def refresh_card(self):
        self.card = build_card(self.idx, self.catalog)
        return self.card

    def refresh_manifest(self):
        """Пересобрать единый паспорт после изменения каталога или смыслового слоя."""
        if self.idx is None or self.card is None or self.catalog is None or self.sem is None:
            self.manifest = None
            return None
        from ..workbook.manifest import build
        self.manifest = build(self)
        return self.manifest

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
