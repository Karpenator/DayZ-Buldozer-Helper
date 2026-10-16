"""Гизмо выделенного объекта в Buldozer: три стрелки осей, за которые тянут мышью (без Qt).

Скрипт моста (v16) рисует стрелки из центра габаритов выделенного объекта — X (восток,
красная), Y (вверх, зелёная), Z (север, синяя) — длиной ``length`` метров (пропорционально
расстоянию до камеры, чтобы на экране они были примерно одного размера) и сообщает, где они
на экране: строка ``GZ|сеанс|номер|ox|oy|xx|xy|yx|yy|zx|zy|длина`` (доли экрана ×1e6,
длина в мм).

Окно переводит доли экрана в пиксели клиентской области окна Buldozer, по курсору находит
стрелку под ним (:func:`axis_at`), а пока тянут — переводит сдвиг мыши в метры вдоль оси
(:class:`AxisDrag`): проекция сдвига на направление стрелки на экране, делённая на «пикселей
на метр» стрелки в момент захвата.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

AXES = ("X", "Y", "Z")
GRAB_PX = 10.0            # курсор ближе к стрелке — её можно взять
MIN_AXIS_PX = 14.0        # стрелка короче (ось смотрит в камеру) — не берётся: слишком резко


@dataclass
class Gizmo:
    """Стрелки на экране (доли экрана 0…1) для объекта ``ref`` сеанса скрипта ``session``."""
    session: int
    ref: int
    origin: tuple[float, float]
    ends: tuple[tuple[float, float], tuple[float, float], tuple[float, float]]
    length: float             # длина стрелок в мире, м


def parse_gizmo(parts: list[str]) -> Gizmo | None:
    """``GZ|…`` (уже разбитая по ``|``) -> :class:`Gizmo` или None."""
    if len(parts) < 12:
        return None
    try:
        v = [int(p) / 1e6 for p in parts[3:11]]
        return Gizmo(int(parts[1]), int(parts[2]), (v[0], v[1]),
                     ((v[2], v[3]), (v[4], v[5]), (v[6], v[7])), int(parts[11]) / 1000.0)
    except ValueError:
        return None


def to_pixels(g: Gizmo, rect: tuple[int, int, int, int]):
    """(начало, [концы X, Y, Z]) в экранных пикселях по клиентской области (x, y, ш, в)."""
    x0, y0, w, h = rect

    def px(p):
        return x0 + p[0] * w, y0 + p[1] * h
    return px(g.origin), [px(e) for e in g.ends]


def _seg_dist(p, a, b) -> float:
    ax, ay = b[0] - a[0], b[1] - a[1]
    n = ax * ax + ay * ay
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / n))
    return math.hypot(p[0] - a[0] - t * ax, p[1] - a[1] - t * ay)


def axis_at(g: Gizmo | None, rect, cursor, grab_px: float = GRAB_PX) -> int | None:
    """Индекс оси (0 X, 1 Y, 2 Z), за стрелку которой можно взять курсором, или None."""
    if g is None or rect is None or cursor is None or rect[2] <= 0 or rect[3] <= 0:
        return None
    o, ends = to_pixels(g, rect)
    best = None
    for i, e in enumerate(ends):
        if math.hypot(e[0] - o[0], e[1] - o[1]) < MIN_AXIS_PX:
            continue
        # у самого центра стрелки сходятся — там берём ту, к которой ближе всего
        d = _seg_dist(cursor, o, e)
        if d <= grab_px and (best is None or d < best[0]):
            best = (d, i)
    return best[1] if best else None


@dataclass
class AxisDrag:
    """Тянут стрелку оси ``axis``: сдвиг мыши (пиксели) -> метры вдоль оси."""
    axis: int
    start: tuple[float, float, float]     # x, z, подъём объекта в момент захвата
    unit: tuple[float, float]             # направление стрелки на экране (единичное)
    px_per_m: float
    total: tuple[float, float] = (0.0, 0.0)

    @classmethod
    def begin(cls, g: Gizmo, rect, axis: int, start: tuple[float, float, float]) -> "AxisDrag | None":
        o, ends = to_pixels(g, rect)
        e = ends[axis]
        dx, dy = e[0] - o[0], e[1] - o[1]
        n = math.hypot(dx, dy)
        if n < MIN_AXIS_PX or g.length <= 0:
            return None
        return cls(axis, start, (dx / n, dy / n), n / g.length)

    def add(self, dx: float, dy: float) -> None:
        self.total = (self.total[0] + dx, self.total[1] + dy)

    @property
    def meters(self) -> float:
        return (self.total[0] * self.unit[0] + self.total[1] * self.unit[1]) / self.px_per_m

    def values(self) -> tuple[float, float, float]:
        """Новые (x, z, подъём): меняется только величина своей оси."""
        x, z, lift = self.start
        m = self.meters
        if self.axis == 0:
            return x + m, z, lift
        if self.axis == 1:
            return x, z, lift + m
        return x, z + m, lift
