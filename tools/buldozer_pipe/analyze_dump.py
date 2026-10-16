"""Разбор дампов канала Buldozer <-> Terrain Builder.

Дампы пишут ``frida_capture.py`` (перехват реального трафика TB <-> Buldozer) и
``probe_pipe.py`` (наш собственный канал: создаём pipe сами и запускаем Buldozer с
``-connect=pipe\\<токен>,<магия>``). Формат — JSON Lines, по одной записи на операцию:

    {"t": 1.234, "proc": "DayZ_x64.exe", "dir": "rx", "path": "\\\\?\\pipe\\ABC,8EDB",
     "handle": 1560, "data": "0011aabb...", "note": "NtReadFile"}

Скрипт ничего не знает о смысле протокола: он показывает, как поток режется на
сообщения, что стоит в начале сообщений и как выглядят числа внутри. Этого хватает,
чтобы понять фрейминг, а дальше — собирать клиент.

Запуск (примеры)::

    python analyze_dump.py pipe_capture_*.log                 # обзор всех потоков
    python analyze_dump.py dump.log --show 5                  # + первые сообщения
    python analyze_dump.py dump.log --hex 64                  # больше байт в превью
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

__all__ = [
    "Record",
    "channel_summary",
    "Stream",
    "read_records",
    "group_streams",
    "candidate_framings",
    "describe_payload",
    "float_preview",
    "format_report",
]

_SIZES = {1: "B", 2: "H", 4: "I", 8: "Q"}


@dataclass
class Record:
    """Одна операция чтения/записи в канал (или открытие канала)."""

    t: float
    proc: str
    direction: str                     # rx | tx | open
    path: str
    handle: int
    data: bytes
    note: str = ""

    @classmethod
    def from_json(cls, obj: dict) -> "Record":
        raw = obj.get("data") or ""
        try:
            data = bytes.fromhex(raw)
        except ValueError:
            data = b""
        return cls(
            t=float(obj.get("t") or 0.0),
            proc=str(obj.get("proc") or "?"),
            direction=str(obj.get("dir") or "?"),
            path=str(obj.get("path") or ""),
            handle=int(obj.get("handle") or 0),
            data=data,
            note=str(obj.get("note") or ""),
        )


@dataclass
class Stream:
    """Непрерывный поток одного направления (процесс + канал + направление)."""

    proc: str
    direction: str
    path: str
    records: list[Record] = field(default_factory=list)

    @property
    def data(self) -> bytes:
        return b"".join(r.data for r in self.records)

    @property
    def nbytes(self) -> int:
        return sum(len(r.data) for r in self.records)

    @property
    def span(self) -> tuple[float, float]:
        if not self.records:
            return (0.0, 0.0)
        return (min(r.t for r in self.records), max(r.t for r in self.records))

    def title(self) -> str:
        name = self.path or f"handle {self.records[0].handle if self.records else 0:#x}"
        action = {"rx": "принято", "tx": "отправлено",
                  "open": "открытие канала"}.get(self.direction, self.direction)
        return f"{self.proc}: {name} [{action}]"


def read_records(paths: Iterable[Path]) -> list[Record]:
    """Прочитать все дампы; битые строки пропускаются с предупреждением в stderr."""
    out: list[Record] = []
    for path in paths:
        for lineno, obj in _iter_json_objects(path):
            try:
                out.append(Record.from_json(obj))
            except (TypeError, ValueError) as exc:
                print(f"{path}:{lineno}: строка пропущена ({exc})", file=sys.stderr)
    return out


def _iter_json_objects(path: Path) -> Iterator[tuple[int, dict]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                print(f"{path}:{lineno}: не JSON, пропуск", file=sys.stderr)
                continue
            if isinstance(obj, dict):
                yield lineno, obj


def group_streams(records: Iterable[Record], merge_handles: bool = True) -> list[Stream]:
    """Сгруппировать записи в потоки: процесс + канал + направление.

    ``merge_handles`` склеивает записи с одинаковым путём канала (у TB и Buldozer
    один и тот же pipe виден под разными handle).
    """
    # Имя канала могло выясниться позже первых записей (NtQueryObject): соберём карту
    # «процесс + дескриптор -> имя канала» и подставим её записям без имени.
    names: dict[tuple[str, int], str] = {}
    for rec in records:
        if rec.path and rec.handle:
            names[(rec.proc, rec.handle)] = rec.path
    streams: dict[tuple[str, str, str], Stream] = {}
    unknown: dict[tuple[str, str, str], Stream] = {}
    for rec in records:
        path = rec.path or names.get((rec.proc, rec.handle), "")
        if path:
            key = (rec.proc, rec.direction, path)
            stream = streams.get(key)
            if stream is None:
                stream = streams[key] = Stream(rec.proc, rec.direction, path)
        else:
            key2 = (rec.proc, rec.direction, str(rec.handle))
            stream = unknown.get(key2)
            if stream is None:
                stream = unknown[key2] = Stream(rec.proc, rec.direction, "")
        stream.records.append(rec)
    result = list(streams.values()) + list(unknown.values())
    for stream in result:
        stream.records.sort(key=lambda r: r.t)
    return sorted(result, key=lambda s: (s.proc, s.direction, s.path))


@dataclass
class FramingGuess:
    """Гипотеза о разбиении потока на сообщения."""

    header_bytes: int
    endian: str
    messages: int
    payload_bytes: int
    consumed: int
    description: str

    @property
    def fraction(self) -> float:
        return self.consumed / max(1, self._total)

    _total: int = 0


def candidate_framings(stream: bytes, max_header: int = 16) -> list[FramingGuess]:
    """Подобрать длину заголовка и порядок байт так, чтобы поток ровно резался.

    Перебираем классику: в начале сообщения лежит его длина (1/2/4/8 байт, little/big
    endian), а иногда ещё и постоянный заголовок (``extra`` байт) до/после длины.
    """
    guesses: list[FramingGuess] = []
    if len(stream) < 4:
        return guesses
    for size in (4, 2, 8, 1):
        code = _SIZES[size]
        for endian in ("<", ">"):
            for extra in range(0, max_header + 1):
                messages, payload, consumed = _walk(stream, size, endian, extra)
                if messages < 2:
                    continue
                guess = FramingGuess(
                    header_bytes=extra + size,
                    endian="little" if endian == "<" else "big",
                    messages=messages,
                    payload_bytes=payload,
                    consumed=consumed,
                    description=(f"длина {size} Б ({endian}, "
                                 f"{'little' if endian == '<' else 'big'} endian)"
                                 + (f" + постоянный заголовок {extra} Б" if extra else "")),
                )
                guess._total = len(stream)
                guesses.append(guess)
    guesses.sort(key=lambda g: (g.fraction, g.messages), reverse=True)
    return guesses[:8]


def _walk(stream: bytes, size: int, endian: str, extra: int) -> tuple[int, int, int]:
    code = _SIZES[size]
    offset = 0
    messages = 0
    payload = 0
    while offset + extra + size <= len(stream):
        (length,) = struct.unpack_from(endian + code, stream, offset + extra)
        if length <= 0 or offset + extra + size + length > len(stream):
            break
        offset += extra + size + length
        messages += 1
        payload += length
    return messages, payload, offset


def float_preview(data: bytes, limit: int = 32) -> list[float]:
    """Первые f32-значения, которые выглядят как координаты/высоты, а не как мусор."""
    out: list[float] = []
    for (value,) in struct.iter_unpack("<f", data[: limit * 4]):
        if math.isfinite(value) and 1e-4 <= abs(value) <= 1e6:
            out.append(round(value, 3))
    return out


def describe_payload(data: bytes, limit: int = 64) -> str:
    """Hex + ASCII + числа — минимальный «отпечаток» начала сообщения."""
    head = data[:limit]
    hexed = " ".join(f"{b:02x}" for b in head)
    ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
    ints = list(struct.unpack_from("<" + _SIZES[4] * (len(head) // 4), head)) if len(head) >= 4 else []
    floats = float_preview(head)
    parts = [f"hex: {hexed}"]
    if ints:
        parts.append("u32: " + " ".join(str(v) for v in ints[:12]))
    if floats:
        parts.append("f32: " + " ".join(f"{v:g}" for v in floats[:12]))
    parts.append(f"ascii: {ascii_}")
    return "\n      ".join(parts)


def channel_summary(streams: list[Stream], magic: str = "8EDB") -> str:
    """Строка «поймали ли мы канал»: потоки, похожие на канал TB <-> Buldozer."""
    if not streams:
        return "В дампе нет записей."
    ours = [s for s in streams if magic.lower() in (s.path or "").lower()]
    named = {s.path for s in streams if s.path}
    if ours:
        total = sum(s.nbytes for s in ours)
        return (f"Канал найден: {len(ours)} поток(ов) с «{magic}», {total} Б. "
                f"Пути: {', '.join(sorted({s.path for s in ours}))}")
    if named:
        return (f"Каналов с «{magic}» нет. Именованных каналов в дампе: {len(named)} "
                f"(например, {sorted(named)[0]}). "
                "Значит, канал TB ещё не открывался при этой записи.")
    return ("Именованных каналов в дампе нет вообще: весь трафик шёл по дескрипторам, "
            "имя которых выяснить не удалось.")


def format_report(streams: list[Stream], show: int = 0, hex_bytes: int = 64,
                  framings: int = 4, magic: str = "8EDB") -> str:
    """Текстовый отчёт по всем потокам: объём, фрейминг, начало сообщений."""
    lines: list[str] = [channel_summary(streams, magic), ""]
    if not streams:
        return lines[0]
    for stream in streams:
        data = stream.data
        t0, t1 = stream.span
        lines.append(f"== {stream.title()}")
        lines.append(f"   записей: {len(stream.records)}, байт: {len(data)}, "
                     f"время: {t1 - t0:.2f} с ({t0:.2f}..{t1:.2f})")
        if data:
            first = data[:16]
            lines.append(f"   первые байты: {' '.join(f'{b:02x}' for b in first)}")
            for guess in candidate_framings(data)[:framings]:
                lines.append(f"   фрейминг? {guess.description}: "
                             f"{guess.messages} сообщений, покрытие "
                             f"{guess.fraction * 100:.1f}%")
        for rec in stream.records[:show]:
            lines.append(f"   t={rec.t:8.3f} {rec.note or rec.direction} "
                         f"({len(rec.data)} Б)")
            if rec.data:
                lines.append("      " + describe_payload(rec.data, hex_bytes))
        if show and len(stream.records) > show:
            lines.append(f"   … ещё {len(stream.records) - show} записей")
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Разбор дампов канала Buldozer <-> TB")
    parser.add_argument("dumps", nargs="+", type=Path, help="файлы *.log (JSON Lines)")
    parser.add_argument("--show", type=int, default=0,
                        help="показать первые N записей каждого потока")
    parser.add_argument("--hex", dest="hex_bytes", type=int, default=64,
                        help="сколько байт печатать в превью записи (по умолчанию 64)")
    parser.add_argument("--out", type=Path, default=None, help="сохранить отчёт в файл")
    parser.add_argument("--magic", default="8EDB",
                        help="вторая часть имени канала TB (по умолчанию 8EDB)")
    args = parser.parse_args(argv)

    records = read_records(args.dumps)
    report = format_report(group_streams(records), show=args.show,
                           hex_bytes=args.hex_bytes, magic=args.magic)
    print(report)
    if args.out is not None:
        args.out.write_text(report + "\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
