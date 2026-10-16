"""Поиск подсказок о канале прямо в бинарниках (DayZ_x64.exe, TerrainBuilder.exe).

Идея простая: если движок умеет ``-connect=pipe\\...``, то в его коде есть строки об
этом — имена параметров, текст ошибок, названия сообщений, константа ``8EDB``. Строки
дешевле и быстрее любого дизассемблера и часто сразу объясняют протокол.

Примеры::

    python strings_scan.py "E:\\Steam\\steamapps\\common\\DayZ\\DayZ_x64.exe" --filter buld
    python strings_scan.py DayZ_x64.exe --filter "pipe|connect|tool" --min-len 6
    python strings_scan.py TerrainBuilder.exe --filter "8EDB|connect=|\\bpipe\\b"
    python strings_scan.py DayZ_x64.exe --utf16 --filter terrain --top 200

Скрипт ничего не меняет на диске и не требует прав администратора.
"""

from __future__ import annotations

import argparse
import mmap
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator

__all__ = ["iter_strings", "scan_file", "scan_bytes", "compile_filter", "ASCI", "UNICODE"]

ASCI = "ascii"
UNICODE = "utf16"

DEFAULT_FILTER = r"buld|terrain|tbconnect|\bpipe\b|connect=|8EDB"


def compile_filter(pattern: str, ignore_case: bool = True) -> re.Pattern[str]:
    """Собрать регулярное выражение из строки пользователя (с понятной ошибкой)."""
    try:
        return re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise SystemExit(f"плохое регулярное выражение {pattern!r}: {exc}") from exc


def iter_strings(data: bytes, encoding: str = ASCI, min_len: int = 5) -> Iterator[tuple[int, str]]:
    """Все печатные строки по возрастанию смещения (ascii или utf-16le)."""
    if encoding == ASCI:
        pattern = re.compile(rb"[\x20-\x7e]{%d,}" % min_len)
        for match in pattern.finditer(data):
            yield match.start(), match.group().decode("ascii", "replace")
    else:
        pattern = re.compile(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len)
        for match in pattern.finditer(data):
            raw = match.group()
            text = raw.decode("utf-16le", "replace")
            yield match.start(), text


def scan_bytes(data: bytes, pattern: re.Pattern[str], encoding: str = ASCI,
               min_len: int = 5, top: int = 0) -> list[tuple[int, str]]:
    """Отфильтрованные строки (с сохранением порядка и обрезкой по ``top``)."""
    found: list[tuple[int, str]] = []
    for offset, text in iter_strings(data, encoding, min_len):
        if pattern.search(text):
            found.append((offset, text))
            if top and len(found) >= top:
                break
    return found


def scan_file(path: Path, pattern: re.Pattern[str], encoding: str = ASCI, min_len: int = 5,
              top: int = 0, chunk: int = 64 * 1024 * 1024) -> list[tuple[int, str]]:
    """Скан большого файла кусками (чтобы не держать в памяти гигабайты)."""
    out: list[tuple[int, str]] = []
    with path.open("rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            step = chunk if chunk > 0 else len(mapped)
            start = 0
            while start < len(mapped):
                # перекрытие, чтобы строки на стыке не терялись
                stop = min(len(mapped), start + step + 4096)
                piece = mapped[start:stop]
                for offset, text in iter_strings(piece, encoding, min_len):
                    if pattern.search(text):
                        out.append((start + offset, text))
                        if top and len(out) >= top:
                            return out
                start = stop
    return out


def dedupe(matches: Iterable[tuple[int, str]], keep: int = 0) -> list[tuple[int, str]]:
    """Убрать повторы (одна и та же строка встречается в бинарнике десятки раз)."""
    counter: Counter[str] = Counter(text for _, text in matches)
    seen: set[str] = set()
    out: list[tuple[int, str]] = []
    for offset, text in matches:
        if text in seen:
            continue
        seen.add(text)
        out.append((offset, text))
        if keep and len(out) >= keep:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Строки бинарника по фильтру")
    parser.add_argument("files", nargs="+", type=Path, help="файлы для сканирования")
    parser.add_argument("--filter", default=DEFAULT_FILTER, help="регулярка по строкам")
    parser.add_argument("--min-len", type=int, default=5, help="минимальная длина строки")
    parser.add_argument("--top", type=int, default=0, help="остановиться после N совпадений")
    parser.add_argument("--utf16", action="store_true", help="искать и UTF-16LE строки")
    parser.add_argument("--no-dedupe", action="store_true", help="не убирать повторы")
    parser.add_argument("--out", type=Path, default=None, help="сохранить результат в файл")
    args = parser.parse_args(argv)

    pattern = compile_filter(args.filter)
    lines: list[str] = []
    for path in args.files:
        if not path.is_file():
            print(f"нет файла: {path}", file=sys.stderr)
            continue
        lines.append(f"== {path} ({path.stat().st_size} Б)")
        for encoding in ([ASCI, UNICODE] if args.utf16 else [ASCI]):
            found = scan_file(path, pattern, encoding, args.min_len, args.top)
            if not args.no_dedupe:
                found = dedupe(found, args.top)
            lines.append(f"-- {encoding}: {len(found)} строк")
            for offset, text in found:
                lines.append(f"   {offset:#010x}  {text}")
        lines.append("")
    report = "\n".join(lines).rstrip()
    print(report)
    if args.out is not None:
        args.out.write_text(report + "\n", encoding="utf-8")
        print(f"\nСохранено: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
