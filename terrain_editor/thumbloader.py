"""Фоновая загрузка превью для списка моделей (Qt).

Готовые превью из кэша берутся сразу. Недостающие рисует один фоновый поток с пониженным
приоритетом (Buldozer в это время работает рядом и не должен проседать). Сначала — то, что
сейчас видно в списке: каждый новый запрос встаёт в начало очереди.
"""
from __future__ import annotations

import sys
import threading
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap

from . import debuglog
from .dayzpaths import texture_resolver
from .thumbcache import ThumbCache

log = debuglog.get_logger("thumbs")

MAX_QUEUE = 400                 # старые (уже прокрученные) запросы отбрасываем


class ThumbnailLoader(QObject):
    ready = Signal(str)          # путь модели (как в шаблоне): превью готово или не получилось

    def __init__(self, cache: ThumbCache | None = None, work_drive: str = "P:\\",
                 parent=None, start_thread: bool = True) -> None:
        super().__init__(parent)
        self.cache = cache or ThumbCache()
        self._resolve_texture = texture_resolver(work_drive)
        self._pixmaps: dict[str, QPixmap | None] = {}     # путь файла (lower) -> картинка / None
        self._queue: deque[tuple[str, Path]] = deque()
        self._queued: set[str] = set()
        self._cv = threading.Condition()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._start_thread = start_thread
        self.rendered = 0

    def set_work_drive(self, work_drive: str) -> None:
        self._resolve_texture = texture_resolver(work_drive)

    # ---- для окна
    def pixmap(self, model: str, path: Path) -> QPixmap | None:
        """Картинка, если уже есть (память или диск). Иначе None и запрос в фон."""
        k = str(path).lower()
        if k in self._pixmaps:
            return self._pixmaps[k]
        state, png, _key = self.cache.lookup(path)
        if state == "ok":
            pm = QPixmap(str(png))
            self._pixmaps[k] = pm if not pm.isNull() else None
            return self._pixmaps[k]
        if state in ("fail", "nofile"):
            self._pixmaps[k] = None
            return None
        self._enqueue(model, path)
        return None

    def known(self, path: Path) -> bool:
        """Уже известно, есть превью или нет (не нужно ждать фон)."""
        return str(path).lower() in self._pixmaps

    def forget(self) -> None:
        """Сбросить картинки в памяти (после очистки кэша)."""
        self._pixmaps.clear()
        with self._cv:
            self._queue.clear()
            self._queued.clear()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    # ---- фон
    def _enqueue(self, model: str, path: Path) -> None:
        k = str(path).lower()
        with self._cv:
            if k in self._queued:
                try:                                   # уже ждёт — поднимем в начало
                    self._queue.remove((model, path))
                except ValueError:
                    pass
            self._queue.appendleft((model, path))
            self._queued.add(k)
            while len(self._queue) > MAX_QUEUE:
                _m, old = self._queue.pop()
                self._queued.discard(str(old).lower())
            self._cv.notify()
        if self._start_thread and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, name="thumbs", daemon=True)
            self._thread.start()

    def process_one(self) -> bool:
        """Нарисовать одно превью из очереди в текущем потоке (для тестов). False — очередь пуста."""
        with self._cv:
            if not self._queue:
                return False
            model, path = self._queue.popleft()
            self._queued.discard(str(path).lower())
        self._render(model, path)
        return True

    def _render(self, model: str, path: Path) -> None:
        png = self.cache.render(path, self._resolve_texture)
        self.rendered += 1
        k = str(path).lower()
        if png is None:
            self._pixmaps[k] = None
        else:
            self._pixmaps.pop(k, None)          # QPixmap создадим в потоке окна (pixmap())
        self.ready.emit(model)

    def _run(self) -> None:
        _lower_thread_priority()
        while True:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                model, path = self._queue.popleft()
                self._queued.discard(str(path).lower())
            try:
                self._render(model, path)
            except Exception:                    # поток не должен умирать из-за одной модели
                log.exception("thumbnail %s", path)


def _lower_thread_priority() -> None:
    if not sys.platform.startswith("win"):
        return
    try:                                         # pragma: no cover - нужен Windows
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), -2)    # THREAD_PRIORITY_LOWEST
    except Exception:
        pass
