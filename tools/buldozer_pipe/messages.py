"""Разбор потока на сообщения: типы, размеры, содержимое.

Захват (``frida_capture.py``) пишет сырые чтения/записи канала. Этот скрипт собирает их в
потоки и режет на сообщения, перебирая варианты заголовков:

* ``[u32 тип][u32 длина]`` (и обратный порядок),
* ``[u32 длина]`` без типа,
* варианты с 16-битными полями.

Для лучшего варианта печатается таблица сообщений, гистограмма типов и «превью» тела:
hex, ASCII и первые числа как u32/i32/f32 — этого хватает, чтобы понять, где высоты
рельефа, где пути материалов, а где трансформы.

Примеры::

    python messages.py /tmp/capt/tb.log                       # обзор всех потоков
    python messages.py /tmp/capt/tb.log --stream 0x940 --count 20
    python messages.py /tmp/capt/dayz.log --stream 0xd94 --hex 96
    python messages.py /tmp/capt/tb.log --stream 0x940 --export-dir msgs

Тип сообщения печатается как в файле, а если рядом с ним есть «соседи» по размеру,
видно, какие сообщения повторяются (это обычно плитки или трансформы).
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

sys.path.insert(0, str(Path(__file__).with_name("analyze_dump.py").parent))

from analyze_dump import Record, Stream, group_streams, read_records  # noqa: E402

__all__ = ["Framing", "Message", "FRAMINGS", "detect_framing", "split_messages",
           "format_messages", "type_histogram", "format_writes", "write_code",
           "find_strings", "format_strings"]

MAX_MESSAGE = 32 * 1024 * 1024          # защита от «длины» из мусора

# (название, [(поле, размер)...]) — порядок полей в заголовке сообщения.
FRAMINGS: dict[str, list[tuple[str, int]]] = {
    "len_u32":         [("len", 4)],
    "type4+len4":      [("type", 4), ("len", 4)],
    "len4+type4":      [("len", 4), ("type", 4)],
    "type2+len2":      [("type", 2), ("len", 2)],
    "len2+type2":      [("len", 2), ("type", 2)],
    "type2+len4":      [("type", 2), ("pad", 2), ("len", 4)],
}


@dataclass
class Framing:
    """Вариант разбора потока на сообщения."""

    name: str
    fields: list[tuple[str, int]]
    messages: int = 0
    covered: int = 0
    total: int = 0
    first_bad: int = -1              # смещение, на котором разбор сломался

    @property
    def header(self) -> int:
        return sum(size for _, size in self.fields)

    @property
    def fraction(self) -> float:
        return self.covered / self.total if self.total else 0.0

    def describe(self) -> str:
        parts = []
        for name, size in self.fields:
            if name == "pad":
                parts.append(f"пропуск {size} Б")
            else:
                parts.append(f"{'тип' if name == 'type' else 'длина'} "
                             f"{size * 8} бит")
        return " + ".join(parts)


@dataclass
class Message:
    """Одно сообщение: где лежит, какой тип и какие байты."""

    index: int
    offset: int
    mtype: int | None
    length: int
    body: bytes

    @property
    def size(self) -> int:
        return self.length


def _field_value(data: bytes, offset: int, size: int) -> int:
    if size == 4:
        return struct.unpack_from("<I", data, offset)[0]
    if size == 2:
        return struct.unpack_from("<H", data, offset)[0]
    return data[offset]


def split_messages(data: bytes, framing: Framing, start: int = 0,
                   resync: bool = False) -> Iterator[Message]:
    """Разрезать поток на сообщения; при ``resync`` искать следующий заголовок после сбоя."""
    offset = start
    index = 0
    while offset + framing.header <= len(data):
        values: dict[str, int] = {}
        pos = offset
        for name, size in framing.fields:
            values[name] = _field_value(data, pos, size)
            pos += size
        length = values.get("len", 0)
        if length <= 0 or length > MAX_MESSAGE or pos + length > len(data):
            if not resync:
                return
            offset += 1                       # сдвигаемся на байт и пробуем снова
            continue
        body = data[pos:pos + length]
        yield Message(index=index, offset=offset, mtype=values.get("type"),
                      length=length, body=body)
        index += 1
        offset = pos + length


def _walk(data: bytes, framing: Framing) -> Framing:
    """Пройти поток без восстановления — получить покрытие и число сообщений."""
    result = Framing(framing.name, framing.fields, total=len(data))
    offset = 0
    while offset + framing.header <= len(data):
        values: dict[str, int] = {}
        pos = offset
        for name, size in framing.fields:
            values[name] = _field_value(data, pos, size)
            pos += size
        length = values.get("len", 0)
        if length <= 0 or length > MAX_MESSAGE or pos + length > len(data):
            result.first_bad = len(data) - offset
            break
        result.messages += 1
        offset = pos + length
        result.covered = offset
    return result


def detect_framing(data: bytes, minimum: int = 8) -> list[Framing]:
    """Варианты разбора, отсортированные по покрытию потока."""
    results = []
    for name, fields in FRAMINGS.items():
        framing = Framing(name, fields)
        results.append(_walk(data, framing))
    good = [f for f in results if f.messages >= minimum]
    good.sort(key=lambda f: (f.fraction, f.messages), reverse=True)
    if good:
        return good
    rest = sorted(results, key=lambda f: (f.fraction, f.messages), reverse=True)
    return rest


def type_histogram(messages: Iterable[Message]) -> list[tuple[int | None, int, int, int]]:
    """(тип, сколько штук, минимальный размер, максимальный размер) по убыванию частоты."""
    counts: Counter[int | None] = Counter()
    sizes: dict[int | None, list[int]] = {}
    for msg in messages:
        counts[msg.mtype] += 1
        sizes.setdefault(msg.mtype, []).append(msg.size)
    out = []
    for mtype, count in counts.most_common():
        length = sizes[mtype]
        out.append((mtype, count, min(length), max(length)))
    return out


def _number_preview(body: bytes, limit: int = 10) -> str:
    """Числа из тела: u32/i32/f32 — то, что чаще всего объясняет сообщение."""
    count = min(limit, len(body) // 4)
    if count == 0:
        return ""
    u32 = struct.unpack_from("<" + "I" * count, body)
    i32 = struct.unpack_from("<" + "i" * count, body)
    f32 = struct.unpack_from("<" + "f" * count, body)
    lines = [f"u32: {' '.join(str(v) for v in u32)}",
             f"i32: {' '.join(str(v) for v in i32)}"]
    floats = []
    for value in f32:
        if value != value or abs(value) > 1e9:      # NaN / мусор
            floats.append("—")
        else:
            floats.append(f"{value:.6g}")
    lines.append(f"f32: {' '.join(floats)}")
    return "\n      ".join(lines)


def _string_preview(body: bytes) -> str:
    text = ""
    current = []
    for byte in body:
        if 32 <= byte < 127:
            current.append(chr(byte))
        else:
            if len(current) >= 4:
                text += (" | " if text else "") + "".join(current)
            current = []
    if len(current) >= 4:
        text += (" | " if text else "") + "".join(current)
    return text


def describe_body(body: bytes, hex_bytes: int = 48) -> str:
    head = body[:hex_bytes]
    parts = ["hex: " + " ".join(f"{b:02x}" for b in head)]
    if len(body) > len(head):
        parts[0] += f" … (+{len(body) - len(head)} Б)"
    numbers = _number_preview(body)
    if numbers:
        parts.append(numbers)
    text = _string_preview(body[:256])
    if text:
        parts.append(f"строки: {text}")
    return "\n      ".join(parts)


def find_strings(data: bytes, min_len: int = 4, max_len: int = 512
                 ) -> list[tuple[int, int, str]]:
    """Найти все «[u32 длина][ascii-строка с нулём]» — так протокол носит пути.

    Видно, что сообщение описывает: путь модели (`dz\\structures\\...`), путь слоя
    поверхности (`ca\\desert\\data\\--sea--`), путь проекта (`P:\\evelone2666\\...`).
    """
    out: list[tuple[int, int, str]] = []
    for offset in range(0, len(data) - 5):
        (length,) = struct.unpack_from("<I", data, offset)
        if length < min_len or length > max_len or offset + 4 + length > len(data):
            continue
        raw = data[offset + 4:offset + 4 + length]
        if not raw:
            continue
        # длина у BI не включает завершающий нуль: он идёт отдельным байтом после строки
        text = raw[:-1] if raw[-1] == 0 else raw
        if not text or any(byte < 32 or byte > 126 for byte in text):
            continue
        if b"\\" not in text and b"/" not in text and not text[:1].isalpha():
            continue                      # отсекаем случайные совпадения в числах
        out.append((offset, length, text.decode("ascii", "replace")))
    return out


def format_strings(stream: Stream, count: int = 40) -> str:
    """Список строк потока: смещение, длина, текст."""
    strings = find_strings(stream.data)
    lines = [f"== {stream.title()} — строк с длиной: {len(strings)}"]
    for offset, length, text in strings[:count]:
        lines.append(f"   @{offset:#08x} len={length:5d}  {text[:120]}")
    if len(strings) > count:
        lines.append(f"   … ещё {len(strings) - count}")
    lines.append("")
    return "\n".join(lines)


def write_code(data: bytes) -> int | None:
    """Первый u32 записи — «код» сообщения (у TB это 2, 7, 8, 13, 25...)."""
    if len(data) < 4:
        return None
    return struct.unpack_from("<I", data)[0]


def format_writes(stream: Stream, count: int = 20, hex_bytes: int = 48,
                  histogram: int = 12) -> str:
    """Показать записи по отдельности: размер, код и превью тела.

    Это самый полезный режим для этого канала: и TB, и Buldozer пишут по одному
    сообщению на вызов WriteFile, поэтому границы записей и есть границы сообщений.
    """
    sizes = Counter(len(r.data) for r in stream.records)
    codes = Counter(code for r in stream.records
                    if (code := write_code(r.data)) is not None)
    lines = [f"== {stream.title()} — {stream.nbytes} Б, {len(stream.records)} записей",
             "   размеры записей: " + ", ".join(f"{sz} Б ×{n}" for sz, n in sizes.most_common(8)),
             "   коды записей (первый u32): "
             + ", ".join(f"{code} ×{n}" for code, n in codes.most_common(histogram))]
    for i, rec in enumerate(stream.records[:count]):
        code = write_code(rec.data)
        lines.append(f"   #{i} t={rec.t:.3f} {len(rec.data)} Б, код={code}")
        if rec.data:
            lines.append("      " + describe_body(rec.data, hex_bytes))
    if len(stream.records) > count:
        lines.append(f"   … ещё {len(stream.records) - count} записей")
    lines.append("")
    return "\n".join(lines)


def format_messages(stream: Stream, messages: list[Message], framing: Framing,
                    count: int = 10, hex_bytes: int = 48) -> str:
    lines = [f"== {stream.title()} — {stream.nbytes} Б, {len(stream.records)} записей",
             f"   разбор: {framing.describe()}; сообщений {framing.messages}, "
             f"покрытие {framing.fraction * 100:.1f}%"]
    histogram = type_histogram(messages)
    if histogram:
        lines.append("   типы: " + "; ".join(
            f"тип {t if t is not None else '—'}: {n} шт. ({lo}..{hi} Б)"
            for t, n, lo, hi in histogram[:10]))
    for msg in messages[:count]:
        lines.append(f"   #{msg.index} @{msg.offset:#x} тип={msg.mtype} "
                     f"длина={msg.length}")
        lines.append("      " + describe_body(msg.body, hex_bytes))
    if len(messages) > count:
        lines.append(f"   … ещё {len(messages) - count} сообщений")
    lines.append("")
    return "\n".join(lines)


def _select_streams(streams: list[Stream], only: str) -> list[Stream]:
    if not only:
        return [s for s in streams if s.nbytes > 0]
    wanted = only.lower().replace("0x", "")
    picked = []
    for stream in streams:
        handle = f"{stream.records[0].handle:x}" if stream.records else ""
        if handle == wanted or wanted in (stream.path or "").lower():
            picked.append(stream)
    if not picked:
        print(f"поток {only} не найден; есть: "
              + ", ".join(f"{s.records[0].handle:#x}" for s in streams if s.records),
              file=sys.stderr)
    return picked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Разбор канала на сообщения")
    parser.add_argument("dumps", nargs="+", type=Path, help="дампы *.log (JSON Lines)")
    parser.add_argument("--stream", default="", help="только этот поток: 0x940 или часть имени")
    parser.add_argument("--count", type=int, default=10, help="сколько сообщений показать")
    parser.add_argument("--hex", dest="hex_bytes", type=int, default=48,
                        help="сколько байт тела печатать")
    parser.add_argument("--min-messages", type=int, default=8,
                        help="сколько сообщений минимум нужно варианту разбора")
    parser.add_argument("--strings", action="store_true",
                        help="найти в потоке строки с длиной ([u32 len][ascii]) — "
                             "пути моделей, слоёв и проекта")
    parser.add_argument("--writes", action="store_true",
                        help="показать записи по отдельности (границы сообщений) — "
                             "лучший режим для этого канала")
    parser.add_argument("--framing", default="",
                        help="не подбирать, а взять вариант по имени: "
                             + ", ".join(FRAMINGS))
    parser.add_argument("--export-dir", type=Path, default=None,
                        help="выгрузить тела сообщений в файлы (<тип>_<номер>.bin)")
    parser.add_argument("--out", type=Path, default=None, help="сохранить отчёт в файл")
    args = parser.parse_args(argv)

    records = read_records(args.dumps)
    streams = _select_streams(group_streams(records), args.stream)
    if not streams:
        print("нет данных: проверьте, что канал использовался при записи", file=sys.stderr)
        return 2

    report: list[str] = []
    for stream in streams:
        data = stream.data
        if args.strings:
            report.append(format_strings(stream, args.count))
            continue
        if args.writes:
            report.append(format_writes(stream, args.count, args.hex_bytes))
            continue
        if not data:
            continue
        if args.framing:
            framing = next((Framing(name, fields) for name, fields in FRAMINGS.items()
                            if name == args.framing), None)
            if framing is None:
                print(f"неизвестный вариант разбора: {args.framing}", file=sys.stderr)
                return 2
            best = _walk(data, framing)
            best.total = len(data)
        else:
            candidates = detect_framing(data, args.min_messages)
            if not candidates:
                report.append(f"== {stream.title()} — разбор не нашёлся\n")
                continue
            best = candidates[0]
            if len(candidates) > 1 and args.stream:
                for other in candidates[1:4]:
                    if other.fraction < best.fraction:
                        report.append(f"   (вариант {other.describe()}: "
                                      f"{other.messages} сообщений, "
                                      f"{other.fraction * 100:.1f}%)")
        framing = Framing(best.name, best.fields, messages=best.messages,
                          covered=best.covered, total=best.total)
        messages = list(split_messages(data, framing, resync=False))
        report.append(format_messages(stream, messages, framing, args.count, args.hex_bytes))

        if args.export_dir is not None:
            args.export_dir.mkdir(parents=True, exist_ok=True)
            handle = f"{stream.records[0].handle:x}"
            for msg in messages[:200]:
                name = f"{stream.proc}_{handle}_{stream.direction}_" \
                       f"{msg.index:04d}_t{msg.mtype}_l{msg.length}.bin"
                (args.export_dir / name).write_bytes(msg.body)
            report.append(f"   выгружено тел: {min(len(messages), 200)} "
                          f"-> {args.export_dir}\n")

    text = "\n".join(report).rstrip()
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"\nОтчёт сохранён: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
