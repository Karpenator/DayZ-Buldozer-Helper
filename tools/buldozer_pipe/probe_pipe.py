"""Проба «своего канала»: наш named pipe + Buldozer с ``-connect=pipe\\<токен>,<магия>``.

Зачем: у Terrain Builder с Buldozer есть закрытый канал (TB дописывает движку
``-connect=pipe\\<токен>,8EDB``). Прежде чем писать клиент, надо выяснить две вещи:

* кто в этой паре сервер (кто создаёт pipe: TB или движок);
* что движок присылает первым, если сервером выступаем мы.

Режимы::

    # 1. Мы сервер, Buldozer подключается к нам (самый вероятный вариант)
    python probe_pipe.py listen --launch "E:\\Steam\\steamapps\\common\\DayZ\\DayZ_x64.exe \
        -buldozer -window -noland -nopause -profiles=P:\\Buldozer -name=Buldozer"

    # 2. Мы клиент: движок создал pipe сам, мы к нему стучимся
    python probe_pipe.py connect --token 1A041E

    # 3. Отправить байты в живой канал (когда listen уже работает, во втором окне)
    python probe_pipe.py listen --launch "..." --send "hex 00010203"

    # 4. Какие каналы сейчас есть на машине (ищем наш ,8EDB)
    python probe_pipe.py pipes

    # 5. Подключиться к живому каналу TB вторым клиентом и просто читать
    #    (токен видно в командной строке Buldozer: -connect=pipe\\<токен>,8EDB)
    python probe_pipe.py tap --token 1A041E --seconds 30

Строку ``--launch`` можно взять у Terrain Builder целиком, вместе с его
``-connect=pipe\\<чужой токен>,8EDB``: скрипт заменит токен на свой. Если движок не
подключится за ``--connect-timeout`` (60 с по умолчанию) — скрипт печатает подсказку
и завершается, а не висит в ожидании.

Всё, что пришло/ушло, пишется в JSON Lines (тот же формат, что у ``frida_capture.py``),
поэтому дамп скармливается ``analyze_dump.py``. Режим по умолчанию — ``listen``.

Важно: скрипт работает только на Windows (нужны named pipes). Если игра запускается
через Steam/BattlEye — используйте DayZDiag_x64.exe: он без BattlEye и не рискует
быть принятым за вмешательство извне.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import random
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["PipeProbeError", "build_pipe_name", "connect_param", "open_dump",
           "MAGIC_DEFAULT", "list_pipes", "filter_pipe_names", "parse_send_spec",
           "apply_connect", "find_foreign_connect", "prepare_launch", "TOOL_VERSION"]

# Метка свежести копии инструментов: печатается в начале каждого режима. Пользователь
# сверяет её с датой архива (см. README «Обновить копию из архива без git»).
# При обновлении файлов обновите и дату — она одна и та же и у frida_capture.py.
TOOL_VERSION = "2026-10-12"

MAGIC_DEFAULT = "8EDB"          # вторая часть параметра, у TB она всегда одинаковая
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
ERROR_PIPE_CONNECTED = 535
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 231
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
BUFSIZE = 65536


class PipeProbeError(RuntimeError):
    """Понятная ошибка вместо traceback: на Windows-консоли он бесполезен."""


def build_pipe_name(token: str, magic: str = MAGIC_DEFAULT, with_magic: bool = True) -> str:
    """Имя канала: ``\\\\.\\pipe\\<токен>[,<магия>]`` (как в параметре ``-connect=``)."""
    suffix = f",{magic}" if with_magic and magic else ""
    return rf"\\.\pipe\{token}{suffix}"


def connect_param(token: str, magic: str = MAGIC_DEFAULT, with_magic: bool = True) -> str:
    """Значение для командной строки движка: ``pipe\\<токен>[,<магия>]``."""
    suffix = f",{magic}" if with_magic and magic else ""
    return rf"pipe\{token}{suffix}"


def apply_connect(command: str, param: str) -> tuple[str, int]:
    """Подставить наш ``-connect=`` в командную строку движка; (команда, сколько заменено).

    В строке TB уже есть ``-connect=pipe\\<чужой токен>,8EDB``: если его не заменить,
    движок уйдёт в канал TB, а наш канал будет пуст и скрипт повиснет в ожидании.
    Замена идёт через ``re.subn`` с **лямбдой**: обычная строка-замена трактует
    ``\\0``..``\\9`` из токена как группы regex, а буквенные последовательности вроде
    ``\\F`` — как ошибочные escape-последовательности. Если ``-connect=`` в строке нет,
    параметр дописывается в конец.
    """
    replacement = f"-connect={param}"
    new_command, replaced = re.subn(r"-connect=\S+", lambda _match: replacement,
                                    command, count=1, flags=re.IGNORECASE)
    if replaced == 0:
        sep = "" if not new_command or new_command[-1:].isspace() else " "
        new_command = f"{new_command}{sep}{replacement}"
    return new_command, replaced


def find_foreign_connect(command: str, param: str) -> list[str]:
    """Чужие ``-connect=`` в командной строке (не совпадают с нашим параметром)."""
    mine = f"-connect={param}".lower()
    return [match.group(0) for match in re.finditer(r"-connect=\S+", command,
                                                    flags=re.IGNORECASE)
            if match.group(0).lower() != mine]


def prepare_launch(command: str, param: str) -> tuple[str, int]:
    """Команда запуска с нашим ``-connect=``; понятная ошибка, если остался чужой.

    Именно её вызывает ``cmd_listen``: лучше сразу объяснить, почему движок уйдёт
    в канал Terrain Builder, чем висеть на пустом канале.
    """
    new_command, replaced = apply_connect(command, param)
    foreign = find_foreign_connect(new_command, param)
    if foreign:
        raise PipeProbeError(
            f"в строке запуска остался чужой параметр {foreign[0]}: движок уйдёт в "
            f"канал Terrain Builder, а наш ({param}) будет пуст. Оставьте в --launch "
            "один -connect= (скрипт заменит его на свой) или уберите их совсем.")
    return new_command, replaced


class Dump:
    """Пишет записи канала в JSON Lines (формат совпадает с frida_capture.py)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("w", encoding="utf-8")
        self._t0 = time.monotonic()
        self.count = 0
        self.bytes = 0

    def add(self, direction: str, data: bytes, handle: int = 0, note: str = "",
            pipe: str = "") -> None:
        rec = {
            "t": round(time.monotonic() - self._t0, 4),
            "proc": "probe",
            "dir": direction,
            "path": pipe,
            "handle": int(handle),
            "data": data.hex(),
            "note": note,
        }
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.count += 1
        self.bytes += len(data)
        if direction == "rx":
            head = " ".join(f"{b:02x}" for b in data[:24])
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:24])
            print(f"[{rec['t']:7.2f}] принято {len(data):6d} Б: {head}  |{ascii_}|")

    def close(self) -> None:
        self._fh.close()


def list_pipes() -> list[str]:
    r"""Имена существующих named pipes (Windows): ``\\.\pipe\<имя>`` -> список имён."""
    if sys.platform != "win32":
        return []
    try:                                            # pragma: no cover - Windows
        return sorted(os.listdir("\\\\.\\pipe\\"))
    except OSError:                                 # pragma: no cover
        return []


def filter_pipe_names(names: list[str], needle: str = "", magic: str = MAGIC_DEFAULT) -> list[str]:
    """Оставить каналы, похожие на наш: с магией (``...,8EDB``) или с подстрокой."""
    out = []
    magic_tail = f",{magic.lower()}" if magic else ""
    for name in names:
        lowered = name.lower()
        if magic_tail and lowered.endswith(magic_tail):
            out.append(name)
        elif needle and needle.lower() in lowered:
            out.append(name)
    return out


def parse_send_spec(spec: str) -> bytes:
    """``hex 00 11 aa`` / ``0011aa`` / ``text привет`` -> байты."""
    text = spec.strip()
    if text.lower().startswith("text "):
        return text[5:].encode("utf-8")
    if text.lower().startswith("hex "):
        text = text[4:]
    text = text.replace(",", " ").replace("\n", " ")
    if not text:
        raise PipeProbeError("пустая строка для отправки")
    tokens = text.split()
    if len(tokens) == 1 and len(tokens[0]) % 2 == 0:
        tokens = [tokens[0][i:i + 2] for i in range(0, len(tokens[0]), 2)]
    try:
        return bytes(int(part, 16) for part in tokens)
    except ValueError as exc:
        raise PipeProbeError(f"не разобрал hex: {spec!r}") from exc


# ------------------------------------------------------------------ Win32 через ctypes
if sys.platform == "win32":                     # pragma: no cover - только Windows
    _k32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateNamedPipeW.restype = wt.HANDLE
    _k32.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD,
                                      wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    _k32.ConnectNamedPipe.restype = wt.BOOL
    _k32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
    _k32.CreateFileW.restype = wt.HANDLE
    _k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p]
    _k32.ReadFile.restype = wt.BOOL
    _k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    _k32.WriteFile.restype = wt.BOOL
    _k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                               ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    _k32.WaitNamedPipeW.restype = wt.BOOL
    _k32.WaitNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD]
    _k32.FlushFileBuffers.restype = wt.BOOL
    _k32.FlushFileBuffers.argtypes = [wt.HANDLE]
    _k32.CloseHandle.restype = wt.BOOL
    _k32.CloseHandle.argtypes = [wt.HANDLE]
else:                                            # pragma: no cover
    _k32: Any = None


def _hval(handle: object) -> int:
    """Числовое значение дескриптора Win32 (c_void_p нельзя сравнивать с int напрямую)."""
    value = getattr(handle, "value", None)
    if value is None:
        return int(handle) if isinstance(handle, int) else 0   # pragma: no cover
    return int(value)


def _last_error() -> int:
    """Код последней ошибки Win32 (на не-Windows её просто нет)."""
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


def _require_windows() -> None:
    if _k32 is None:
        raise PipeProbeError("named pipes есть только на Windows; запустите этот скрипт "
                             "на машине с DayZ")


def _launch(command: str) -> subprocess.Popen:
    """Запуск движка. Командная строка разбирается как в Windows (кавычки — двойные)."""
    args = [token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token
            for token in shlex.split(command, posix=False)]
    exe = Path(args[0])
    if not exe.is_file():
        raise PipeProbeError(f"не нашёл exe: {exe}")
    print(f"запускаю: {command}")
    return subprocess.Popen([str(exe), *args[1:]], cwd=str(exe.parent))


def _reader_loop(handle, dump: Dump, stop: threading.Event, pipe_name: str) -> None:
    """Читаем канал, пока он живой (или пока не попросили остановиться)."""
    buf = ctypes.create_string_buffer(BUFSIZE)
    while not stop.is_set():
        read = wt.DWORD(0)
        ok = _k32.ReadFile(handle, buf, BUFSIZE, ctypes.byref(read), None)
        if not ok:
            err = _last_error()
            if err == ERROR_BROKEN_PIPE:
                print("канал закрыт второй стороной")
            elif err:
                print(f"ReadFile: ошибка {err}")
            stop.set()
            return
        if read.value:
            dump.add("rx", buf.raw[: read.value], handle=_hval(handle), note="probe rx",
                     pipe=pipe_name)


def _stdin_loop(handle, dump: Dump, stop: threading.Event, pipe_name: str) -> None:
    """Ручная отправка: строки с консоли в формате ``hex 00 11`` или ``text hello``."""
    for line in sys.stdin:
        if stop.is_set():
            return
        if not line.strip():
            continue
        try:
            data = parse_send_spec(line)
        except PipeProbeError as exc:
            print(f"  {exc}")
            continue
        written = wt.DWORD(0)
        ok = _k32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
        if not ok:
            print(f"WriteFile: ошибка {_last_error()}")
            continue
        dump.add("tx", data, handle=_hval(handle), note="probe tx", pipe=pipe_name)
        print(f"  отправлено {written.value} Б")


def _wait_connection(handle, timeout: float) -> bool:
    """Ждать клиента в отдельном потоке: ``ConnectNamedPipe`` блокируется навсегда.

    True — движок подключился; False — вышел таймаут (``timeout <= 0`` — ждать без
    ограничения). Благодаря таймауту скрипт не висит вечно, а печатает подсказку.
    """
    outcome: list[bool] = []

    def _connect() -> None:
        ok = bool(_k32.ConnectNamedPipe(handle, None))
        if not ok and _last_error() == ERROR_PIPE_CONNECTED:
            ok = True                          # клиент успел подключиться раньше нас
        outcome.append(ok)

    waiter = threading.Thread(target=_connect, daemon=True)
    waiter.start()
    waiter.join(timeout if timeout and timeout > 0 else None)
    return bool(outcome) and outcome[0]


def _print_connect_hint(pipe_name: str, param: str, timeout: float) -> None:
    """Подсказка, когда движок не подключился к нашему каналу за отведённое время."""
    print(f"\nдвижок не подключился за {timeout:g} с. Проверьте по порядку:")
    print(f"  1. наш канал: {pipe_name} — в командной строке движка должен быть "
          f"-connect={param}")
    print("     (чужой -connect= в --launch скрипт теперь заменяет сам; оставьте "
          "в строке запуска только один)")
    print("  2. не ушёл ли движок в канал TB:  py -3 probe_pipe.py pipes --find 8EDB")
    print("  3. токен как у TB: повторите с --token 1A2B3C (6 hex-знаков)")
    print("  4. первый запуск грузится 15–25 с и дольше: --connect-timeout 180")


def cmd_listen(args: argparse.Namespace) -> int:
    """Мы — сервер канала; движок подключается к нам по ``-connect=pipe\\...``."""
    _require_windows()
    token = args.token or f"{random.randrange(0x1000000):06X}"
    pipe_name = build_pipe_name(token, args.magic, not args.no_magic)
    print(f"инструменты от {TOOL_VERSION}")
    print(f"pipe: {pipe_name}")
    handle = _k32.CreateNamedPipeW(pipe_name, PIPE_ACCESS_DUPLEX,
                                   PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                                   1, BUFSIZE, BUFSIZE, 0, None)
    if _hval(handle) == INVALID_HANDLE_VALUE:
        raise PipeProbeError(f"CreateNamedPipeW не удался: ошибка "
                             f"{_last_error()} (канал занят?)")

    dump = Dump(args.out or Path(f"probe_{token}.log"))
    stop = threading.Event()
    proc: subprocess.Popen | None = None
    try:
        param = connect_param(token, args.magic, not args.no_magic)
        if args.launch:
            command, replaced = prepare_launch(args.launch, param)
            print(f"параметр движка: -connect={param}"
                  + (" (заменил чужой)" if replaced else " (добавлен)"))
            proc = _launch(command)
        print(f"жду подключения движка (до {args.connect_timeout:g} с, Ctrl+C — выход)…")
        connected = _wait_connection(handle, args.connect_timeout)
        print("подключился!" if connected else "подключения не было")
        if not connected:
            _print_connect_hint(pipe_name, param, args.connect_timeout)
            return 1
        threading.Thread(target=_reader_loop, args=(handle, dump, stop, pipe_name),
                         daemon=True).start()
        if args.send:
            for spec in args.send:
                data = parse_send_spec(spec)
                written = wt.DWORD(0)
                _k32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
                dump.add("tx", data, handle=_hval(handle), note="probe tx",
                         pipe=pipe_name)
                print(f"отправлено {written.value} Б: {spec}")
        if sys.stdin.isatty():
            threading.Thread(target=_stdin_loop, args=(handle, dump, stop, pipe_name),
                             daemon=True).start()
            print("можно печатать строки: 'hex 00 11 aa' или 'text hello' "
                  "(пустая строка — пропуск)")
        deadline = time.monotonic() + args.seconds
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("остановлено пользователем")
    finally:
        stop.set()
        _k32.CloseHandle(handle)
        dump.close()
        if proc is not None and proc.poll() is None and not args.keep_game:
            print("закрываю движок")
            proc.terminate()
        print(f"\nзаписей: {dump.count}, байт: {dump.bytes}, дамп: {dump.path}")
        print(f"разбор: python analyze_dump.py {dump.path}")
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    """Мы — клиент: проверяем, создаёт ли движок канал сам."""
    _require_windows()
    candidates = [build_pipe_name(args.token, args.magic, True),
                  build_pipe_name(args.token, args.magic, False),
                  rf"\\.\pipe\{args.token}"]
    dump = Dump(args.out or Path(f"probe_connect_{args.token}.log"))
    stop = threading.Event()
    handle = None
    try:
        for name in candidates:
            print(f"пробую {name} …")
            # args.wait приходит float; WaitNamedPipeW требует DWORD — без int()
            # ctypes бросал ArgumentError (баг приёма правок 28 в прошлом сеансе).
            if not _k32.WaitNamedPipeW(name, int(args.wait * 1000)):
                err = _last_error()
                print(f"  нет канала (ошибка {err})")
                continue
            handle = _k32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None,
                                      OPEN_EXISTING, 0, None)
            if _hval(handle) == INVALID_HANDLE_VALUE:
                print(f"  не открылся: ошибка {_last_error()}")
                handle = None
                continue
            print(f"  подключился к {name}")
            threading.Thread(target=_reader_loop, args=(handle, dump, stop, name),
                             daemon=True).start()
            deadline = time.monotonic() + args.seconds
            while not stop.is_set() and time.monotonic() < deadline:
                time.sleep(0.2)
            break
        else:
            print("ни один вариант имени не подошёл — вероятно, сервером выступает TB, "
                  "а не движок (используйте режим listen)")
    except KeyboardInterrupt:
        print("остановлено пользователем")
    finally:
        stop.set()
        if _hval(handle or 0):
            _k32.CloseHandle(handle)
        dump.close()
        print(f"\nзаписей: {dump.count}, байт: {dump.bytes}, дамп: {dump.path}")
    return 0


def cmd_pipes(args: argparse.Namespace) -> int:
    """Показать каналы на машине; отдельно — похожие на канал TB/Buldozer."""
    _require_windows()
    names = list_pipes()
    if not names:
        print("каналов не видно (или это не Windows)")
        return 0
    if getattr(args, "all", False):
        print(f"всего каналов: {len(names)} (полный список)")
        for name in sorted(names, key=str.lower):
            print(f"    \\\\.\\pipe\\{name}")
        return 0
    ours = filter_pipe_names(names, args.find or "", args.magic)
    print(f"всего каналов: {len(names)}")
    if ours:
        print(f"\nпохоже на канал Terrain Builder (…,{args.magic}):")
        for name in ours:
            print(f"    \\.\\pipe\\{name}")
        print("\nподключиться и просто почитать: "
              "python probe_pipe.py tap --token <часть до запятой>")
    else:
        print(f"\nканалов с окончанием ,{args.magic} сейчас нет — "
              "TB с Buldozer, похоже, не соединены")
    if args.find:
        extra = [n for n in names if args.find.lower() in n.lower() and n not in ours]
        if extra:
            print(f"\nещё по подстроке {args.find!r}:")
            for name in extra[:40]:
                print(f"    {name}")
    return 0


def cmd_tap(args: argparse.Namespace) -> int:
    """Подключиться к живому каналу TB вторым клиентом и только читать.

    Это разведка «без Frida»: если сервер канала (TB) разрешает больше одного клиента,
    мы увидим тот же поток, что и Buldozer. Скрипт ничего не пишет, если не передан
    ``--send`` — то есть вмешаться в работу TB он не может.
    """
    _require_windows()
    name = build_pipe_name(args.token, args.magic, not args.no_magic)
    dump = Dump(args.out or Path(f"tap_{args.token}.log"))
    stop = threading.Event()
    handle = None
    try:
        if not _k32.WaitNamedPipeW(name, int(args.wait * 1000)):
            err = _last_error()
            hint = ("канал занят (обычно у него только один экземпляр) — подключиться "
                    "вторым клиентом нельзя" if err == ERROR_PIPE_BUSY else
                    "канал не найден — проверьте токен (он в командной строке Buldozer)")
            print(f"не дождался канала {name}: ошибка {err}\n  {hint}")
            return 0
        handle = _k32.CreateFileW(name, GENERIC_READ | (GENERIC_WRITE if args.send else 0),
                                  0, None, OPEN_EXISTING, 0, None)
        if _hval(handle) == INVALID_HANDLE_VALUE:
            print(f"не открылся: ошибка {_last_error()} "
                  "(прав не хватает? запустите консоль от администратора)")
            return 0
        print(f"подключился к {name} — читаю {args.seconds:g} с "
              "(Ctrl+C — раньше). В живой канал ничего не пишу.")
        threading.Thread(target=_reader_loop, args=(handle, dump, stop, name),
                         daemon=True).start()
        for spec in args.send:
            data = parse_send_spec(spec)
            written = wt.DWORD(0)
            _k32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
            dump.add("tx", data, handle=_hval(handle), note="tap tx", pipe=name)
            print(f"отправлено {written.value} Б (--send): {spec}")
        deadline = time.monotonic() + args.seconds
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("остановлено пользователем")
    finally:
        stop.set()
        if _hval(handle or 0):
            _k32.CloseHandle(handle)
        dump.close()
        print(f"\nзаписей: {dump.count}, байт: {dump.bytes}, дамп: {dump.path}")
        if dump.bytes:
            print("похоже, канал отдаёт поток и второму клиенту — это самая ценная запись!\n"
                  f"разбор: python analyze_dump.py {dump.path}")
        else:
            print("данных нет: либо сервер не пускает второго клиента к данным, "
                  "либо поток уже закончился (TB передаёт мир в начале сеанса)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Проба канала Buldozer <- TB: свой named pipe и запуск движка")
    sub = parser.add_subparsers(dest="mode")

    listen = sub.add_parser("listen", help="мы сервер канала, движок подключается к нам")
    listen.add_argument("--token", default="",
                        help="токен канала (по умолчанию случайный, 6 hex-знаков как у TB)")
    listen.add_argument("--magic", default=MAGIC_DEFAULT, help="вторая часть, у TB всегда 8EDB")
    listen.add_argument("--no-magic", action="store_true", help="без ,<магия> в имени канала")
    listen.add_argument("--launch", default="",
                        help="командная строка DayZ/Buldozer; чужой -connect= будет "
                             "заменён на наш, своего может не быть вовсе")
    listen.add_argument("--connect-timeout", type=float, default=60.0,
                        help="сколько секунд ждать подключения движка (по умолчанию 60; "
                             "0 — без ограничения)")
    listen.add_argument("--send", action="append", default=[],
                        help="что отправить сразу: 'hex 00 11' или 'text hello' (можно много раз)")
    listen.add_argument("--seconds", type=float, default=30.0, help="сколько ждать (по умолчанию 30)")
    listen.add_argument("--keep-game", action="store_true", help="не закрывать движок в конце")
    listen.add_argument("--out", type=Path, default=None, help="куда писать дамп")

    pipes = sub.add_parser("pipes", help="какие каналы есть на машине (ищем наш ,8EDB)")
    pipes.add_argument("--magic", default=MAGIC_DEFAULT)
    pipes.add_argument("--find", default="", help="ещё и по подстроке в имени")
    pipes.add_argument("--all", action="store_true",
                       help="печатать ВСЕ каналы подряд (для сверки с handle_names.py)")

    tap = sub.add_parser("tap", help="подключиться к живому каналу TB и только читать")
    tap.add_argument("--token", required=True,
                     help="имя канала (точное — смотрите в выводе handle_names.py)")
    tap.add_argument("--magic", default=MAGIC_DEFAULT)
    tap.add_argument("--no-magic", action="store_true")
    tap.add_argument("--wait", type=float, default=5.0, help="ждать канал, секунд")
    tap.add_argument("--seconds", type=float, default=30.0)
    tap.add_argument("--send", action="append", default=[],
                     help="(опасно) что-нибудь отправить: 'hex 00 11' (по умолчанию не пишем)")
    tap.add_argument("--out", type=Path, default=None)

    connect = sub.add_parser("connect", help="мы клиент: движок уже создал канал")
    connect.add_argument("--token", required=True,
                         help="имя канала (точное — смотрите в выводе handle_names.py)")
    connect.add_argument("--magic", default=MAGIC_DEFAULT)
    connect.add_argument("--wait", type=float, default=5.0, help="ждать канал, секунд")
    connect.add_argument("--seconds", type=float, default=15.0)
    connect.add_argument("--out", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.mode is None:
        args.mode = "listen"
        args.token = ""
        args.magic = MAGIC_DEFAULT
        args.no_magic = False
        args.launch = ""
        args.connect_timeout = 60.0
        args.send = []
        args.seconds = 30.0
        args.keep_game = False
        args.out = None
    try:
        if args.mode == "listen":
            return cmd_listen(args)
        if args.mode == "pipes":
            return cmd_pipes(args)
        if args.mode == "tap":
            return cmd_tap(args)
        return cmd_connect(args)
    except PipeProbeError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ошибка ввода-вывода: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
