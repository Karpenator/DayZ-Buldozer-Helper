"""Object placement list in the Terrain Builder text format.

One object per line, as written by DayZ Editor's EditorTerrainBuilderFile:
    "model";x;z;yaw;pitch;roll;scale;y
* X is written with a +200000 offset (fixed-width trick in the exporter), so the
  real X is value - 200000 when the value is >= 200000.
* yaw/pitch/roll are degrees. y is the object's base height.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

X_OFFSET = 200000.0


@dataclass
class TBObject:
    model: str
    x: float
    z: float
    yaw: float
    pitch: float
    roll: float
    scale: float
    y: float


class ObjectListError(ValueError):
    pass


def _parse_x(text: str) -> float:
    v = float(text)
    return v - X_OFFSET if v >= X_OFFSET else v


def parse_line(line: str, lineno: int = 0) -> TBObject:
    s = line.strip()
    if not s.startswith('"'):
        raise ObjectListError(f"line {lineno}: model name must be quoted")
    end = s.find('";', 1)
    if end < 0:
        raise ObjectListError(f"line {lineno}: missing closing quote")
    model = s[1:end]
    parts = s[end + 2:].split(";")
    if len(parts) != 7:
        raise ObjectListError(f"line {lineno}: expected 8 fields, got {len(parts) + 1}")
    try:
        x = _parse_x(parts[0])
        z = float(parts[1])
        yaw, pitch, roll = float(parts[2]), float(parts[3]), float(parts[4])
        scale = float(parts[5])
        y = float(parts[6])
    except ValueError as exc:
        raise ObjectListError(f"line {lineno}: bad number ({exc})") from None
    return TBObject(model, x, z, yaw, pitch, roll, scale, y)


def _num(v: float) -> str:
    """Number for the text file: up to 6 decimals, no trailing zeros, never '-0'."""
    text = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def format_line(obj: TBObject, offset: tuple[float, float] | None = None) -> str:
    """One line in the same format parse_line reads.

    ``offset`` — start of the Terrain Builder map frame (x, z); by default the DayZ Editor one,
    +200000 on X (TB's default frame). Projects with another frame (``*.tv4p``) need theirs.
    """
    ox, oz = offset if offset is not None else (X_OFFSET, 0.0)
    return ";".join([f'"{obj.model}"', _num(obj.x + ox), _num(obj.z + oz), _num(obj.yaw),
                     _num(obj.pitch), _num(obj.roll), _num(obj.scale), _num(obj.y)])


def format_objects(objects, offset: tuple[float, float] | None = None) -> str:
    """Whole list, one line per object, Windows line ends (Terrain Builder is a Windows tool)."""
    return "".join(format_line(o, offset) + "\r\n" for o in objects)


def parse_objects(text: str) -> tuple[list[TBObject], list[str]]:
    """Parse the whole list. Returns (objects, error messages); bad lines are skipped."""
    objects: list[TBObject] = []
    errors: list[str] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            objects.append(parse_line(line, n))
        except ObjectListError as exc:
            errors.append(str(exc))
    return objects, errors


def load_objects(path: str | Path) -> tuple[list[TBObject], list[str]]:
    return parse_objects(Path(path).read_text(encoding="utf-8", errors="replace"))
