"""Проект Terrain Builder (``*.tv4p``) и общий разбор файлов ALB1 (без Qt).

Файлы TB (``.tv4p`` — проект, ``.tv4l`` — слой объектов, ``.tv4s`` — фигуры) — двоичный «ALB1»:
заголовок ``ALB1`` + 12 байт, таблица имён полей (секция 2), таблица имён классов (секция 3),
затем корневой объект ``u16 класс, u32 указатель`` и его поля ``u16 поле, u8 тип, значение``.

Типы значений (разобраны по проекту Evelone2666):

====  ===========================================================================
0x01  char (1 байт)            0x05/0x06  int32       0x07  uint32
0x08  4 байта (как есть)       0x09       bool (1)    0x0a  float32
0x0b  строка ``u16 длина``     0x13       null        0x14  float64
0x15  ``u8 n`` + n×float64     0x0e       ссылка ``u16 класс, u32 указатель``
0x0c  блок ``u32 размер``: обычно список ``u32 n`` + n полей (``item``/``pair``), но
      дерево объектов слоя (``tree``) — свой формат (см. :mod:`tv4l`)
0x0d  объект ``u32 размер, u16 класс, u32 указатель`` + поля
0x0f  пара: ровно два поля (``key`` и ``data``)
====  ===========================================================================

Что нужно для правки объектов карты (проверено по ``Evelone2666.wrp``: все 5443 объекта):

* **рамка карты** ``objManager.mapFrame.mframe[0].mpos`` = (x0, z0, x1, z1) — координаты
  Buldozer = координаты TB − (x0, z0);
* **сдвиг центра модели** ``objManager.mobjectsDelta``: путь модели → ``SObjectCenterDelta(x, z)``.
  TB хранит в слое **центр** объекта, Buldozer/WRP — начало модели:
  ``слой = начало + (x0, z0) − R(курс)·delta``, где ``R(ψ)(dx, dz) = (dx·cosψ + dz·sinψ,
  −dx·sinψ + dz·cosψ)`` (как матрица объекта DayZ: ось X модели = (cosψ, 0, −sinψ),
  ось Z = (sinψ, 0, cosψ)). Масштаб на delta не влияет.
"""
from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"ALB1"


class AlbError(ValueError):
    pass


@dataclass
class AlbObject:
    cls: str
    fields: list[tuple[str, object]] = field(default_factory=list)

    def get(self, name: str, default=None):
        for k, v in self.fields:
            if k == name:
                return v
        return default

    def all(self, name: str) -> list:
        return [v for k, v in self.fields if k == name]


def name_table(data: bytes, pos: int) -> tuple[dict[int, str], int]:
    """Таблица имён ``u16 секция, u8 тип, u32 количество`` + ``u16 id, u16 длина, текст``."""
    try:
        _sec, _typ, count = struct.unpack_from("<HBI", data, pos)
        pos += 7
        names: dict[int, str] = {}
        for _ in range(count):
            i, n = struct.unpack_from("<HH", data, pos)
            names[i] = data[pos + 4:pos + 4 + n].decode("latin-1")
            pos += 4 + n
    except struct.error as exc:
        raise AlbError("файл обрывается в таблице имён") from exc
    return names, pos


class AlbReader:
    """Разбор значений ALB1. Блоки из ``raw`` (например ``tree``) не разбираются — байты как есть."""

    def __init__(self, data: bytes, raw: tuple[str, ...] = ("tree",)) -> None:
        if data[:4] != MAGIC:
            raise AlbError("не файл Terrain Builder (нет ALB1)")
        self.d = data
        self.raw = raw
        self.fields, pos = name_table(data, 16)
        self.classes, pos = name_table(data, pos)
        self.body = pos

    def root(self, stop: str | None = None) -> AlbObject:
        """Корневой объект. ``stop`` — остановиться перед этим полем (дальше не нужно)."""
        try:
            cl, _ptr = struct.unpack_from("<HI", self.d, self.body)
            obj = AlbObject(self.classes.get(cl, str(cl)))
            self._fields(obj, self.body + 6, len(self.d), stop)
        except (struct.error, IndexError) as exc:
            raise AlbError("файл обрывается") from exc
        return obj

    def top_spans(self) -> list[tuple[str, int, int, int, int]]:
        """Поля корневого объекта: (имя, тип, начало поля, начало значения, конец значения).

        Поля идут подряд от :attr:`body` + 6 до конца файла — по ним файл можно собрать заново,
        заменив отдельные значения.
        """
        spans = []
        pos = self.body + 6
        try:
            while pos < len(self.d):
                fid, t = struct.unpack_from("<HB", self.d, pos)
                name = self.fields.get(fid, f"#{fid}")
                _value, end = self._value(name, t, pos + 3)
                spans.append((name, t, pos, pos + 3, end))
                pos = end
        except (struct.error, IndexError) as exc:
            raise AlbError("файл обрывается") from exc
        if pos != len(self.d):
            raise AlbError("поля не сходятся с размером файла")
        return spans

    def _fields(self, obj: AlbObject, pos: int, end: int, stop: str | None = None) -> int:
        while pos < end:
            fid, t = struct.unpack_from("<HB", self.d, pos)
            name = self.fields.get(fid, f"#{fid}")
            if name == stop:
                return pos
            value, pos = self._value(name, t, pos + 3)
            obj.fields.append((name, value))
        if pos != end:
            raise AlbError("поля объекта не сходятся с его размером")
        return pos

    def _value(self, name: str, t: int, pos: int):
        d = self.d
        if t == 0x01:
            return chr(d[pos]), pos + 1
        if t in (0x05, 0x06):
            return struct.unpack_from("<i", d, pos)[0], pos + 4
        if t == 0x07:
            return struct.unpack_from("<I", d, pos)[0], pos + 4
        if t == 0x08:
            return d[pos:pos + 4], pos + 4
        if t == 0x09:
            return bool(d[pos]), pos + 1
        if t == 0x0A:
            return struct.unpack_from("<f", d, pos)[0], pos + 4
        if t == 0x14:
            return struct.unpack_from("<d", d, pos)[0], pos + 8
        if t == 0x15:
            n = d[pos]
            return list(struct.unpack_from(f"<{n}d", d, pos + 1)), pos + 1 + 8 * n
        if t == 0x0B:
            n = struct.unpack_from("<H", d, pos)[0]
            return d[pos + 2:pos + 2 + n].decode("utf-8", errors="replace"), pos + 2 + n
        if t == 0x13:
            return None, pos
        if t == 0x0E:
            cl, _ptr = struct.unpack_from("<HI", d, pos)
            return AlbObject(self.classes.get(cl, str(cl))), pos + 6
        if t == 0x0D:
            size = struct.unpack_from("<I", d, pos)[0]
            cl, _ptr = struct.unpack_from("<HI", d, pos + 4)
            obj = AlbObject(self.classes.get(cl, str(cl)))
            self._fields(obj, pos + 10, pos + 4 + size)
            return obj, pos + 4 + size
        if t == 0x0F:
            out = []
            for _ in range(2):
                fid, tt = struct.unpack_from("<HB", d, pos)
                value, pos = self._value(self.fields.get(fid, f"#{fid}"), tt, pos + 3)
                out.append(value)
            return tuple(out), pos
        if t == 0x0C:
            size = struct.unpack_from("<I", d, pos)[0]
            end = pos + 4 + size
            if name in self.raw or size < 4:
                return d[pos + 4:end], end
            items = AlbObject("list")
            try:
                self._fields(items, pos + 8, end)
            except (AlbError, struct.error, IndexError):
                return d[pos + 4:end], end            # не список — оставить байты
            return [v for _k, v in items.fields], end
        raise AlbError(f"неизвестный тип 0x{t:02x} у поля {name} (байт {pos - 3})")


def norm_model(path: str) -> str:
    """Путь модели для сравнения: нижний регистр, обратные слэши, без диска/ведущего слэша."""
    p = (path or "").strip().strip('"').replace("/", "\\").lower()
    if len(p) > 1 and p[1] == ":":
        p = p[2:]
    return p.lstrip("\\")


def rotate_delta(dx: float, dz: float, yaw_deg: float) -> tuple[float, float]:
    """R(курс)·delta — вектор delta модели в мировых осях (как матрица объекта DayZ)."""
    a = math.radians(yaw_deg)
    c, s = math.cos(a), math.sin(a)
    return dx * c + dz * s, -dx * s + dz * c


@dataclass
class TbProject:
    path: Path | None
    name: str
    frame: tuple[float, float, float, float]          # x0, z0, x1, z1 (координаты TB)
    deltas: dict[str, tuple[float, float]]            # norm_model -> (dx, dz)
    libs: list[str] = field(default_factory=list)

    @classmethod
    def from_bytes(cls, data: bytes, path: Path | None = None) -> "TbProject":
        root = AlbReader(data).root()
        om = root.get("objManager")
        if not isinstance(om, AlbObject):
            raise AlbError("в проекте нет objManager")
        frame = None
        mf = om.get("mapFrame")
        frames = mf.get("mframe") if isinstance(mf, AlbObject) else None
        for fr in frames or []:
            pos = fr.get("mpos") if isinstance(fr, AlbObject) else None
            if isinstance(pos, list) and len(pos) == 4:
                frame = tuple(float(v) for v in pos)
                break
        if frame is None:
            raise AlbError("в проекте нет рамки карты (mapFrame)")
        deltas: dict[str, tuple[float, float]] = {}
        for pair in om.get("mobjectsDelta") or []:
            if isinstance(pair, tuple) and isinstance(pair[1], AlbObject):
                deltas[norm_model(str(pair[0]))] = (float(pair[1].get("x", 0.0)), float(pair[1].get("z", 0.0)))
        libs = [v for v in (root.get("libs") or []) if isinstance(v, str)]
        return cls(path, str(root.get("mname") or (path.stem if path else "")), frame, deltas, libs)

    @classmethod
    def load(cls, path: str | Path) -> "TbProject":
        p = Path(path)
        return cls.from_bytes(p.read_bytes(), p)

    @property
    def origin(self) -> tuple[float, float]:
        return self.frame[0], self.frame[1]

    def delta(self, model: str) -> tuple[float, float] | None:
        return self.deltas.get(norm_model(model))

    def to_layer(self, x: float, z: float, yaw: float, model: str) -> tuple[float, float]:
        """Начало модели в Buldozer -> центр объекта в слое TB."""
        dx, dz = self.delta(model) or (0.0, 0.0)
        rx, rz = rotate_delta(dx, dz, yaw)
        return x + self.frame[0] - rx, z + self.frame[1] - rz

    def from_layer(self, lx: float, lz: float, yaw: float, model: str) -> tuple[float, float]:
        """Центр объекта в слое TB -> начало модели в Buldozer."""
        dx, dz = self.delta(model) or (0.0, 0.0)
        rx, rz = rotate_delta(dx, dz, yaw)
        return lx - self.frame[0] + rx, lz - self.frame[1] + rz


def find_project_file(start: str | Path, max_depth: int = 2) -> Path | None:
    """``*.tv4p`` в папке проекта (или в её подпапках до max_depth, напр. ``TerrainBuilder``).

    Если указана папка слоёв ``<Имя>.Layers`` — проект рядом с ней (``<Имя>.tv4p``).
    """
    root = Path(start) if start else None
    if root is None or not root.exists():
        return None
    if root.is_file():
        return root if root.suffix.lower() == ".tv4p" else None
    if root.name.lower().endswith(".layers"):
        cand = root.with_name(root.name[:-len(".layers")] + ".tv4p")
        if cand.is_file():
            return cand
        root = root.parent
    base = len(root.parts)
    found: list[Path] = []
    for dirpath, dirs, files in os.walk(root):
        here = Path(dirpath)
        found += [here / f for f in files if f.lower().endswith(".tv4p")]
        if found or len(here.parts) - base >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = sorted(d for d in dirs if not d.lower().endswith((".cache", ".layers", ".shapes"))
                             and d.lower() not in ("data", "world", "templatelibs"))
    if not found:
        return None
    top = min(len(p.parts) for p in found)
    found = sorted((p for p in found if len(p.parts) == top), key=lambda p: p.name.lower())
    if len(found) == 1:
        return found[0]
    last = last_tb_project(found[0].parent)
    for p in found:
        if last and p.name.lower() == last.lower():
            return p                      # последний открытый в TB (Dialogs_new.vdat)

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return max(found, key=mtime)          # иначе — сохранённый последним


def last_tb_project(folder: str | Path) -> str:
    """Имя файла проекта, который TB открывал последним (``Dialogs_new.vdat``: строка
    ``dtProject``, на следующей — путь). Пусто, если не найдено."""
    try:
        lines = (Path(folder) / "Dialogs_new.vdat").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for i, line in enumerate(lines[:-1]):
        if line.strip() == "dtProject":
            value = lines[i + 1].strip()
            return value.replace("/", "\\").rsplit("\\", 1)[-1] if value else ""
    return ""


def load_project(start: str | Path) -> TbProject | None:
    path = find_project_file(start)
    if path is None:
        return None
    try:
        return TbProject.load(path)
    except (OSError, AlbError):
        return None
