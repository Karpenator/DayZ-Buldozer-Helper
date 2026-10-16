"""Кто и какие каналы (named pipes) держит: имена хэндлов и второй конец канала.

Зачем: frida-захват показывает трафик канала TB ↔ Buldozer, но имена хэндлов там
пустые — канал открыт до подключения агента, а NtQueryObject на «занятом» канале
блокируется. Перечислитель имён (``probe_pipe.py pipes``) при этом не показывает
канал ``<токен>,8EDB`` вообще — и непонятно, как он называется и существует ли.

Этот скрипт отвечает на оба вопроса сразу:

* дублирует хэндлы процесса к себе (дубликат не имеет висящих операций — запрос
  имени не блокируется) и спрашивает имя через NtQueryObject;
* берёт из таблицы хэндлов **адрес объекта ядра**: у двух концов одного канала он
  одинаковый — поэтому анонимный канал тоже видно, кто с кем связан.

Запуск (нужен Windows; для чужих процессов — консоль «от администратора»)::

    # все процессы с «dayz» в имени (DayZ_x64, DayZTerrainEditor, DayZToolsLauncher)
    python handle_names.py

    # конкретный процесс
    python handle_names.py --pid 25512

    # имена процессов-подстроки (можно повторять) и фильтр имён каналов
    python handle_names.py --name dayz --name terrain --find 8EDB

Вывод: группы «концов» канала (один объект ядра = один канал), для каждого —
процесс, номер хэндла, класс (именованный канал / анонимный / сокет) и имя.
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import sys
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

__all__ = ["HandleEntry", "classify_name", "group_by_object", "format_report",
           "match_process", "HandleScanError"]

# Номера классов информации для NtQuerySystemInformation / NtQueryObject
SystemExtendedHandleInformation = 64
ObjectNameInformation = 1
PROCESS_DUP_HANDLE = 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DUPLICATE_SAME_ACCESS = 0x0002
FILE_TYPE_PIPE = 3
TH32CS_SNAPPROCESS = 2
MAX_PATH = 260


class HandleScanError(RuntimeError):
    """Понятная ошибка вместо traceback."""


TOOL_VERSION = "2026-10-12"


@dataclass
class HandleEntry:
    """Один хэндл процесса: номер, объект ядра и (позже) имя/класс."""

    pid: int
    process: str
    handle: int
    object: int
    name: str = ""
    file_type: int = 0
    scanned: bool = False           # дублировали и спрашивали имя
    access: str = ""                # причина, если не получилось
    info: str = ""                  # конец/состояние канала (FilePipeLocalInformation)

    @property
    def handle_hex(self) -> str:
        return f"0x{self.handle:X}"


@dataclass
class ObjectGroup:
    """Все хэндлы, указывающие на один объект ядра (например, оба конца канала)."""

    object: int
    entries: list[HandleEntry] = field(default_factory=list)


def classify_name(name: str) -> str:
    """Класс объекта по имени из NtQueryObject.

    Анонимный канал и сокет имени не имеют — их различает только контекст, поэтому
    оба показываются как «без имени».
    """
    lowered = name.lower()
    if not name:
        return "без имени (анонимный канал или сокет)"
    if "\\device\\namedpipe\\" in lowered:
        suffix = lowered.split("namedpipe", 1)[1].strip("\\")
        if not suffix:
            return "без имени (анонимный канал или сокет)"
        return "именованный канал"
    if lowered.startswith("\\device\\afd") or lowered.startswith("\\device\\tcp") \
            or lowered.startswith("\\device\\udp"):
        return "сокет"
    if lowered.startswith("\\device\\"):
        return "устройство"
    return "имя"


def match_process(name: str, patterns: list[str]) -> bool:
    """Подходит ли процесс под одну из подстрок-фильтров (регистр не важен)."""
    if not patterns:
        return True
    lowered = name.lower()
    return any(p.lower() in lowered for p in patterns)


def group_by_object(entries: list[HandleEntry]) -> list[ObjectGroup]:
    """Сгруппировать хэндлы по объекту ядра: один объект = один канал.

    Группы с несколькими держателями («оба конца») идут первыми.
    """
    buckets: dict[int, ObjectGroup] = {}
    for entry in entries:
        buckets.setdefault(entry.object, ObjectGroup(entry.object)).entries.append(entry)
    groups = list(buckets.values())
    groups.sort(key=lambda g: (-len(g.entries), -g.object))
    return groups


def format_report(groups: list[ObjectGroup], only_pipes: bool = True,
                  find: str = "") -> str:
    """Текстовый отчёт по группам; only_pipes — скрывать файлы/прочее."""
    lines: list[str] = []
    shown = 0
    for group in groups:
        entries = [e for e in group.entries if not only_pipes or e.file_type == FILE_TYPE_PIPE]
        if find:
            entries = [e for e in entries if find.lower() in e.name.lower()
                       or find.lower() in e.process.lower()]
        if not entries:
            continue
        shown += 1
        holders = {(e.pid, e.process) for e in entries}
        title = "концы одного канала" if len(holders) > 1 else "хэндл"
        lines.append(f"[{title}] объект 0x{group.object:X}")
        for e in entries:
            note = classify_name(e.name) if e.scanned else f"не спрошено ({e.access})"
            name = e.name if e.name else "—"
            extra = f"  [{e.info}]" if e.info else ""
            lines.append(f"    {e.process} (PID {e.pid})  хэндл {e.handle} ({e.handle_hex})"
                         f"  {note}: {name}{extra}")
    if shown == 0:
        return "ничего не найдено (нужна консоль «от администратора» для чужих процессов)"
    return "\n".join(lines)


# ------------------------------------------------------------------ Win32 через ctypes
if sys.platform == "win32":                     # pragma: no cover - только Windows
    _k32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll: Any = ctypes.WinDLL("ntdll", use_last_error=True)

    _k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    _k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    _k32.Process32FirstW.restype = ctypes.c_int
    _k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _k32.Process32NextW.restype = ctypes.c_int
    _k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    _k32.DuplicateHandle.restype = ctypes.c_int
    _k32.DuplicateHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                     ctypes.c_uint32]
    _k32.GetFileType.restype = ctypes.c_uint32
    _k32.GetFileType.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = ctypes.c_int
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    _ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_uint32, ctypes.c_void_p,
                                                ctypes.c_uint32, ctypes.c_void_p]
    _ntdll.NtQueryObject.restype = ctypes.c_long
    _ntdll.NtQueryObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.c_void_p]
    _ntdll.NtQueryInformationFile.restype = ctypes.c_long
    _ntdll.NtQueryInformationFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.c_uint32,
                                              ctypes.c_uint32]
    _k32.GetFileInformationByHandleEx.restype = ctypes.c_int
    _k32.GetFileInformationByHandleEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                  ctypes.c_void_p, ctypes.c_uint32]

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort),
                    ("MaximumLength", ctypes.c_ushort),
                    ("Buffer", ctypes.c_void_p)]

    class _HANDLE_ENTRY(ctypes.Structure):
        _fields_ = [("Object", ctypes.c_void_p),
                    ("UniqueProcessId", ctypes.c_size_t),
                    ("HandleValue", ctypes.c_size_t),
                    ("GrantedAccess", ctypes.c_uint32),
                    ("CreatorBackTraceIndex", ctypes.c_ushort),
                    ("ObjectTypeIndex", ctypes.c_ushort),
                    ("HandleAttributes", ctypes.c_uint32),
                    ("Reserved", ctypes.c_uint32)]

    class _PROCESS_ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32),
                    ("cntUsage", ctypes.c_uint32),
                    ("th32ProcessID", ctypes.c_uint32),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", ctypes.c_uint32),
                    ("cntThreads", ctypes.c_uint32),
                    ("th32ParentProcessID", ctypes.c_uint32),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", ctypes.c_uint32),
                    ("szExeFile", ctypes.c_wchar * MAX_PATH)]
else:                                           # pragma: no cover
    _k32 = _ntdll = None
    _UNICODE_STRING = _HANDLE_ENTRY = _PROCESS_ENTRY = None


def _require_windows() -> None:
    if _k32 is None:
        raise HandleScanError("инструмент работает только на Windows: нужны "
                              "named pipes и таблица хэндлов")


def list_processes() -> list[tuple[int, str]]:
    """(PID, имя) всех процессов — через Toolhelp, как в диспетчере задач."""
    _require_windows()                          # pragma: no cover
    snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:   # pragma: no cover
        raise HandleScanError("CreateToolhelp32Snapshot не удался")
    out: list[tuple[int, str]] = []
    entry = _PROCESS_ENTRY()
    entry.dwSize = ctypes.sizeof(_PROCESS_ENTRY)
    try:
        ok = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            ok = _k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snapshot)
    return out


def _raw_handle_table() -> tuple[Any, int]:
    """Вся таблица хэндлов системы (NtQuerySystemInformation, класс 64)."""
    size = 1 << 20
    for _ in range(8):                          # pragma: no cover - Windows
        buf = ctypes.create_string_buffer(size)
        ret = ctypes.c_uint32(0)
        status = _ntdll.NtQuerySystemInformation(SystemExtendedHandleInformation,
                                                 buf, size, ctypes.byref(ret))
        if status == 0:
            return buf, size
        size *= 2
    raise HandleScanError("таблица хэндлов не читается")   # pragma: no cover


def _parse_entries(buf: Any, size: int, pids: set[int]) -> list[tuple[int, int, int]]:
    """(pid, handle, object) для хэндлов выбранных процессов."""
    base = ctypes.addressof(buf)
    header_size = 2 * ctypes.sizeof(ctypes.c_size_t)   # NumberOfHandles + Reserved
    count = int.from_bytes((buf.raw[:8]), sys.byteorder)
    entry_size = ctypes.sizeof(_HANDLE_ENTRY)
    out: list[tuple[int, int, int]] = []
    for i in range(count):                      # pragma: no cover - Windows
        offset = base + header_size + i * entry_size
        entry = _HANDLE_ENTRY.from_address(offset)
        pid = int(entry.UniqueProcessId)
        if pid not in pids:
            continue
        out.append((pid, int(entry.HandleValue), int(entry.Object or 0)))
    return out


def _query_name_fileinfo(dup_handle) -> str:
    """Имя файла/канала через GetFileInformationByHandleEx(FileNameInfo).

    В отличие от NtQueryObject НЕ зависает на каналах с висящим чтением
    (именно из-за этого «занятые» каналы раньше показывались безымянными).
    """
    buf = ctypes.create_string_buffer(1024)
    if not _k32.GetFileInformationByHandleEx(dup_handle, 2, buf, len(buf)):
        return ""
    length = int.from_bytes(buf.raw[:4], sys.byteorder)   # FILE_NAME_INFO.FileNameLength (в байтах)
    if length <= 0 or length > 1000:
        return ""
    return buf.raw[4:4 + length].decode("utf-16-le", errors="replace")


def _query_name(dup_handle, timeout: float) -> str:
    """Имя объекта хэндла: FileNameInfo, при неудаче — NtQueryObject (с таймаутом)."""
    name = _query_name_fileinfo(dup_handle)
    if name:
        return name
    result: list[str] = []

    def _run() -> None:
        try:
            buf = ctypes.create_string_buffer(1024)
            ret = ctypes.c_uint32(0)
            status = _ntdll.NtQueryObject(dup_handle, ObjectNameInformation,
                                          buf, len(buf), ctypes.byref(ret))
            if status != 0:
                return
            us = _UNICODE_STRING.from_buffer_copy(buf.raw[:ctypes.sizeof(_UNICODE_STRING)])
            if not us.Length or not us.Buffer:
                result.append("")
                return
            raw = ctypes.string_at(us.Buffer, us.Length)
            result.append(raw.decode("utf-16-le", errors="replace"))
        except Exception:                       # pragma: no cover
            result.append("")

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    return result[0] if result else ""


def _pipe_info(dup_handle) -> str:
    """Конец и состояние канала (FILE_PIPE_LOCAL_INFORMATION): клиент/сервер, режим."""
    buf = ctypes.create_string_buffer(64)
    iosb = ctypes.create_string_buffer(16)
    status = _ntdll.NtQueryInformationFile(dup_handle, iosb, buf, 48,
                                           24)      # FilePipeLocalInformation = 24
    if status != 0:
        return ""
    fields = struct.unpack_from("<10I", buf.raw)
    _type, config, _maxi, _cur, _inq, _read, _outq, _wq, state, pipe_end = fields
    ends = {0: "конец: клиент", 1: "конец: сервер"}.get(pipe_end, f"конец: {pipe_end}")
    states = {1: "отключён", 2: "на подключении", 3: "подключён"}.get(state, f"состояние: {state}")
    duplex = {1: "вход", 2: "выход", 3: "дуплекс"}.get(config, "")
    return "; ".join(x for x in (ends, states, duplex) if x)


def scan(selected: list[tuple[int, str]], timeout: float = 2.0) -> list[HandleEntry]:
    """Хэндлы-каналы выбранных процессов: дублируем, GetFileType, имя."""
    _require_windows()                          # pragma: no cover
    pids = {pid for pid, _ in selected}
    names = {pid: name for pid, name in selected}
    buf, size = _raw_handle_table()
    raw = _parse_entries(buf, size, pids)
    entries = [HandleEntry(pid=pid, process=names.get(pid, f"PID {pid}"),
                           handle=handle, object=obj)
               for pid, handle, obj in raw]

    proc_handles: dict[int, Any] = {}
    for entry in entries:                       # pragma: no cover - Windows
        if entry.pid not in proc_handles:
            proc_handles[entry.pid] = _k32.OpenProcess(
                PROCESS_DUP_HANDLE | PROCESS_QUERY_LIMITED_INFORMATION, 0, entry.pid)
        proc = proc_handles[entry.pid]
        if not proc:
            entry.access = "нет доступа (запустите от администратора)"
            continue
        dup = ctypes.c_void_p()
        if not _k32.DuplicateHandle(proc, ctypes.c_void_p(entry.handle),
                                    ctypes.c_void_p(_k32.GetCurrentProcess()),
                                    ctypes.byref(dup), 0, 0, DUPLICATE_SAME_ACCESS):
            entry.access = f"не дублируется, ошибка {ctypes.get_last_error()}"
            continue
        try:
            entry.file_type = int(_k32.GetFileType(dup))
            if entry.file_type == FILE_TYPE_PIPE:
                entry.name = _query_name(dup, timeout)
                entry.info = _pipe_info(dup)
                entry.scanned = True
        finally:
            _k32.CloseHandle(dup)
    for proc in proc_handles.values():
        if proc:
            _k32.CloseHandle(proc)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Имена каналов процесса и второй конец канала (по объекту ядра)")
    parser.add_argument("--name", action="append", default=[],
                        help="подстрока имени процесса (можно повторять; по умолчанию dayz)")
    parser.add_argument("--pid", action="append", type=int, default=[],
                        help="PID процесса (можно повторять)")
    parser.add_argument("--find", default="",
                        help="показать только имена каналов/процессы с подстрокой")
    parser.add_argument("--all-handles", action="store_true",
                        help="показать и не-каналы (файлы и прочее)")
    parser.add_argument("--timeout", type=float, default=2.0,
                        help="таймаут запроса имени одного хэндла, сек")
    parser.add_argument("--all", action="store_true",
                        help="смотреть ВСЕ процессы (второй конец канала может быть "
                             "в неожиданном процессе)")
    parser.add_argument("--out", type=Path, default=None,
                        help="куда писать отчёт (UTF-8 с BOM — открывается Блокнотом)")
    args = parser.parse_args(argv)
    try:
        processes = list_processes()
        if args.all:
            selected = ([(pid, name) for pid, name in processes
                         if pid in set(args.pid)] if args.pid else processes)
        else:
            patterns = args.name or ["dayz"]
            selected = [(pid, name) for pid, name in processes
                        if pid in set(args.pid) or match_process(name, patterns)]
        if not selected:
            print(f"процессы по фильтру {args.pid} не найдены")
            return 1
        print("процессы: " + ", ".join(f"{name} ({pid})" for pid, name in selected))
        entries = scan(selected, timeout=args.timeout)
        pipes = [e for e in entries if e.file_type == FILE_TYPE_PIPE]
        print(f"хэндлов: {len(entries)}, из них каналов: {len(pipes)}\n")
        groups = group_by_object(entries)
        report = format_report(groups, only_pipes=not args.all_handles, find=args.find)
        print(report)
        if args.out:
            Path(args.out).write_text(report + "\n", encoding="utf-8-sig")
            print(f"\nотчёт сохранён: {args.out}")
    except HandleScanError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
