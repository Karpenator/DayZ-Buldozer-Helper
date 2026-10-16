"""Библиотеки шаблонов Terrain Builder (TemplateLibs/*.tml) — список моделей для окна (без Qt).

В папке проекта карты (например ``P:\\Evelone2666``) где-то лежит папка ``TemplateLibs``
с файлами ``*.tml`` — это библиотеки, подключённые в Terrain Builder::

    <Library name="Evelone_modular" ...>
        <Template>
            <Name>EV_1</Name>                          имя шаблона (так его зовёт TB)
            <File>evelone_modular\\1\\ev_1.p3d</File>    путь модели от корня рабочего диска P:\\
            <BoundingMin X=".." Y=".." Z=".." /> ...
        </Template>
    </Library>

Окно над Buldozer показывает именно эти модели: путь ``File`` уходит в Buldozer (скрипт
создаёт модель по нему), а ``Name`` — в файл экспорта для TB (TB ищет объект по имени шаблона).
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from . import debuglog

log = debuglog.get_logger("templates")

TEMPLATE_DIR = "templatelibs"
TML_EXT = ".tml"
# Папки, в которых библиотек точно нет, а файлов бывает очень много (ускоряет поиск).
_SKIP_DIRS = {".git", "__pycache__", "data", "layers", "textures", "tiles", "backup", "backups"}
_MAX_DEPTH = 6


@dataclass(frozen=True)
class Template:
    name: str                  # имя шаблона TB: EV_1
    file: str                  # путь модели от корня рабочего диска: evelone_modular\1\ev_1.p3d
    library: str               # имя библиотеки (атрибут name у <Library>, иначе имя файла .tml)
    bbox_min: tuple[float, float, float] | None = None
    bbox_max: tuple[float, float, float] | None = None

    @property
    def key(self) -> str:
        return model_key(self.file)


@dataclass
class TemplateLibrary:
    name: str
    path: Path
    templates: list[Template] = field(default_factory=list)
    error: str = ""


def model_key(path: str) -> str:
    """Ключ для сравнения путей моделей: без регистра, через «\\», без ведущих слешей."""
    return path.replace("/", "\\").strip().strip("\\").lower()


def find_template_libs(project_dir: str | Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    """Все *.tml из папок TemplateLibs внутри проекта (папку ищем сами, на любой глубине до max_depth).

    Если сама project_dir — это TemplateLibs, берём файлы прямо из неё.
    """
    root = Path(project_dir)
    if not root.is_dir():
        return []
    found: list[Path] = []
    if root.name.lower() == TEMPLATE_DIR:
        return sorted((p for p in root.iterdir() if p.suffix.lower() == TML_EXT and p.is_file()),
                      key=lambda p: p.name.lower())
    base_depth = len(root.parts)
    for dirpath, dirs, files in os.walk(root):
        here = Path(dirpath)
        depth = len(here.parts) - base_depth
        if here.name.lower() == TEMPLATE_DIR:
            found += [here / f for f in files if f.lower().endswith(TML_EXT)]
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS or d.lower() == TEMPLATE_DIR]
    return sorted(found, key=lambda p: (str(p.parent).lower(), p.name.lower()))


def _vec(el) -> tuple[float, float, float] | None:
    if el is None:
        return None
    try:
        return (float(el.get("X")), float(el.get("Y")), float(el.get("Z")))
    except (TypeError, ValueError):
        return None


_TEMPLATE_RE = re.compile(r"<Template>(.*?)</Template>", re.S | re.I)
_TAG_RE = {tag: re.compile(rf"<{tag}>(.*?)</{tag}>", re.S | re.I) for tag in ("Name", "File")}
_LIBNAME_RE = re.compile(r'<Library\b[^>]*\bname="([^"]*)"', re.I)


def parse_tml_text(text: str, fallback_name: str = "") -> TemplateLibrary:
    """Разобрать текст .tml. Испорченный XML читаем «по-простому» регулярными выражениями."""
    lib = TemplateLibrary(fallback_name, Path(fallback_name or "."))
    try:
        root = ET.fromstring(text.lstrip("\ufeff"))
    except ET.ParseError as exc:
        lib.error = f"XML: {exc}"
        m = _LIBNAME_RE.search(text)
        lib.name = (m.group(1).strip() if m else "") or fallback_name
        for block in _TEMPLATE_RE.findall(text):
            name = _TAG_RE["Name"].search(block)
            file = _TAG_RE["File"].search(block)
            if name and file and file.group(1).strip():
                lib.templates.append(Template(name.group(1).strip(), file.group(1).strip(), lib.name))
        return lib
    lib.name = (root.get("name") or "").strip() or fallback_name
    for t in root.iter("Template"):
        name = (t.findtext("Name") or "").strip()
        file = (t.findtext("File") or "").strip()
        if not file:
            continue
        if not name:
            name = PureWindowsPath(file).stem
        lib.templates.append(Template(name, file, lib.name, _vec(t.find("BoundingMin")),
                                      _vec(t.find("BoundingMax"))))
    return lib


def parse_tml(path: str | Path) -> TemplateLibrary:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return TemplateLibrary(path.stem, path, error=str(exc))
    text = None
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lib = parse_tml_text(text or "", path.stem)
    lib.path = path
    return lib


class TemplateCatalog:
    """Все шаблоны проекта: список для окна + поиск имени шаблона по пути модели."""

    def __init__(self) -> None:
        self.project_dir: Path | None = None
        self.libraries: list[TemplateLibrary] = []
        self.templates: list[Template] = []
        self._by_key: dict[str, Template] = {}
        self.revision = 0              # растёт при каждой загрузке (окно перерисовывает список)

    def load(self, project_dir: str | Path | None) -> int:
        """Найти TemplateLibs в проекте и прочитать все .tml. Возвращает число шаблонов."""
        self.project_dir = Path(project_dir) if project_dir else None
        self.libraries = []
        if self.project_dir is not None:
            for path in find_template_libs(self.project_dir):
                lib = parse_tml(path)
                if lib.error:
                    log.warning("tml %s: %s", path, lib.error)
                self.libraries.append(lib)
        self._rebuild()
        log.info("template catalog %s: %d libraries, %d templates", self.project_dir,
                 len(self.libraries), len(self.templates))
        return len(self.templates)

    def set_libraries(self, libraries: list[TemplateLibrary]) -> None:
        self.libraries = list(libraries)
        self._rebuild()

    def _rebuild(self) -> None:
        seen: set[tuple[str, str]] = set()
        self.templates = []
        self._by_key = {}
        for lib in self.libraries:
            for t in lib.templates:
                ident = (t.library.lower(), t.key)
                if ident in seen:
                    continue
                seen.add(ident)
                self.templates.append(t)
                self._by_key.setdefault(t.key, t)
        self.templates.sort(key=lambda t: (t.library.lower(), _natural(t.name)))
        self.revision += 1

    def library_names(self) -> list[str]:
        return sorted({t.library for t in self.templates}, key=str.lower)

    def by_model(self, model: str) -> Template | None:
        return self._by_key.get(model_key(model))

    def tb_names(self) -> dict[str, str]:
        """{ключ пути модели: имя шаблона} — для экспорта в TB."""
        return {k: t.name for k, t in self._by_key.items()}

    def filtered(self, text: str = "", library: str = "") -> list[Template]:
        words = text.lower().split()
        out = []
        for t in self.templates:
            if library and t.library != library:
                continue
            hay = f"{t.name} {t.file}".lower()
            if all(w in hay for w in words):
                out.append(t)
        return out


def _natural(text: str) -> list:
    """EV_2 раньше EV_10."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]
