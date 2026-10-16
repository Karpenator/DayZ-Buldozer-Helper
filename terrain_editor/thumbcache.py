"""Кэш превью моделей на диске (без Qt).

Превью рисуется один раз и сохраняется PNG в ``%USERPROFILE%\\.dayz_terrain_editor\\thumbs``.
Имя файла — хэш от пути модели, её размера и времени изменения: изменили модель — превью
нарисуется заново, иначе берётся готовое. Модели, которые прочитать не удалось (например,
бинаризованные ODOL), помечаются файлом ``.fail`` и не пересчитываются при каждом запуске.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable

from . import debuglog

log = debuglog.get_logger("thumbs")

CACHE_DIR = Path.home() / ".dayz_terrain_editor" / "thumbs"
RENDER_SIZE = 192              # рисуем один раз крупно, значки любого размера — уменьшение
THUMB_VERSION = 1              # поменять, если изменится рендер: старые превью не подойдут


class ThumbCache:
    def __init__(self, folder: str | Path = CACHE_DIR, size: int = RENDER_SIZE) -> None:
        self.folder = Path(folder)
        self.size = int(size)

    def key(self, model_path: str | Path) -> str | None:
        """Ключ превью или None, если файла модели нет."""
        try:
            st = os.stat(model_path)
        except OSError:
            return None
        text = f"{THUMB_VERSION}|{self.size}|{str(model_path).lower()}|{st.st_size}|{st.st_mtime_ns}"
        return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()

    def png(self, key: str) -> Path:
        return self.folder / key[:2] / f"{key}.png"

    def fail_marker(self, key: str) -> Path:
        return self.folder / key[:2] / f"{key}.fail"

    def lookup(self, model_path: str | Path) -> tuple[str, Path | None, str | None]:
        """('ok', png, key) | ('fail', None, key) | ('nofile', None, None) | ('todo', None, key)."""
        key = self.key(model_path)
        if key is None:
            return "nofile", None, None
        png = self.png(key)
        if png.is_file():
            return "ok", png, key
        if self.fail_marker(key).is_file():
            return "fail", None, key
        return "todo", None, key

    def render(self, model_path: str | Path,
               resolve_texture: Callable[[str], Path | None] | None = None) -> Path | None:
        """Нарисовать и сохранить превью (вызывается из фонового потока). None — не получилось."""
        from .thumbnails import png_bytes, thumbnail_for_file

        state, png, key = self.lookup(model_path)
        if state == "ok":
            return png
        if key is None:
            return None
        png = self.png(key)
        try:
            png.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("thumb cache folder %s: %s", png.parent, exc)
            return None
        try:
            img = thumbnail_for_file(model_path, self.size, resolve_texture=resolve_texture)
        except Exception as exc:                 # ODOL, битый файл, неизвестный формат
            log.info("no preview for %s: %s", model_path, exc)
            try:
                self.fail_marker(key).write_text(f"{model_path}\n{exc}\n", encoding="utf-8")
            except OSError:
                pass
            return None
        tmp = png.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_bytes(png_bytes(img))
            os.replace(tmp, png)
        except OSError as exc:
            log.warning("thumb write %s: %s", png, exc)
            return None
        return png

    def clear(self) -> int:
        """Удалить все сохранённые превью. Возвращает число удалённых файлов."""
        n = 0
        if not self.folder.is_dir():
            return 0
        for p in self.folder.rglob("*"):
            if p.is_file() and p.suffix in (".png", ".fail", ".tmp"):
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
        return n

    def stats(self) -> tuple[int, int]:
        """(число превью, байт на диске)."""
        if not self.folder.is_dir():
            return 0, 0
        count = size = 0
        for p in self.folder.rglob("*.png"):
            try:
                size += p.stat().st_size
                count += 1
            except OSError:
                pass
        return count, size
