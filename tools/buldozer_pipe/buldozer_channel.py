r"""Канал Buldozer «мы вместо TB»: анонимные пайпы + протокол сообщений.

Как это работает у Terrain Builder (установлено живыми дампами и обходом хэндлов):

* TB делает ``CreatePipe`` (два анонимных канала: TB -> движок и движок -> TB),
  запускает ``DayZ_x64.exe -buldozer ... -connect=pipe\\<метка>,8EDB`` и передаёт
  движку концы каналов унаследованными хэндлами. Никакого именованного канала
  ``\\.\pipe\<метка>,8EDB`` **не существует** (tap даёт ошибку 2, перечисление
  пустое) — параметр ``-connect=`` лишь метка.
* Сообщение = ``[u32 код][данные]``; пишется одним WriteFile, но канал байтовый,
  поэтому на чтении сообщения могут слипаться (в дампе видны 152 Б = два 25 и
  156 Б = 7 + 8 + 8). Размеры сообщений главного канала фиксированы (см.
  ``FIXED_SIZES``), код 28 переменной длины — ``[28][count] + count*16``.
* Движок говорит первым: ``[29][5]`` (рукопожатие, 8 Б). TB отвечает описанием
  мира — 65 536 Б (код 2). Дальше TB шлёт объекты (13, 136 Б), курсор (25, 76 Б)
  и т.д., движок шлёт камеру (6, 72 Б, ~30/с), курсор (25), рельеф (28).

Запуск (нужен Windows, рядом с DayZ)::

    # извлечь эталон мира из frida-дампа (один раз):
    python buldozer_channel.py extract-world --dump capture_DayZ_x64.exe_....log --out world.bin

    # запустить движок на нашем канале и посмотреть, идёт ли связь:
    python buldozer_channel.py probe --exe "E:\\Steam\\steamapps\\common\\DayZ\\DayZ_x64.exe" ^
        --world world.bin --seconds 20 --out P:\\capture3\\channel.log

Ожидание probe: ``[29][5]`` -> отправлен мир -> поток сообщений камеры (код 6).
Дамп пишется в формате frida_capture (его читают ``analyze_dump.py``/``messages.py``).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import random
import shlex
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

__all__ = ["FIXED_SIZES", "Message", "encode_message", "decode_stream", "next_message",
           "extract_world_packet", "world_header", "PipeChannel", "build_connect_args",
           "ChannelError", "TOOL_VERSION"]

TOOL_VERSION = "2026-10-12"

# Размеры сообщений главного канала ЦЕЛИКОМ (вместе с u32 кода). Установлено по
# эталонному дампу (handles 3548/3552): 6 и 7 могут слипаться при чтении.
FIXED_SIZES: dict[int, int] = {
    2: 65536,   # описание мира (уникальный пакет за сеанс)
    5: 4,       # пинг
    6: 72,      # камера: [6] + 17 x f32
    7: 4,       # пинг
    8: 76,      # как курсор по размеру
    13: 136,    # объект: [13][u32][128 Б матрицы и пути]
    18: 76,
    25: 76,     # курсор/кисть: [25][1][0x05000000] + 16 x f32
    29: 8,      # рукопожатие: [29][5]
}
TERRAIN_CODE = 28   # [28][count] + count x (u32 x, u32 y, f32 высота, u32 aux)


class ChannelError(RuntimeError):
    """Понятная ошибка вместо traceback."""


class Message:
    """Одно сообщение канала: код и данные без кода."""

    def __init__(self, code: int, payload: bytes) -> None:
        self.code = code
        self.payload = payload

    @property
    def size(self) -> int:
        return 4 + len(self.payload)

    def __repr__(self) -> str:                    # pragma: no cover - отладка
        return f"Message(code={self.code}, payload={len(self.payload)} Б)"


def encode_message(code: int, payload: bytes = b"") -> bytes:
    """Сообщение в канал: [u32 код][данные] (little-endian)."""
    return struct.pack("<I", code) + payload


def message_size(buf: bytes) -> int | None:
    """Полный размер сообщения в начале buf или None, если данных мало / неизвестный код.

    Размеры фиксированы (FIXED_SIZES); код 28 переменный: [28][count] + count*16.
    """
    if len(buf) < 4:
        return None
    code = struct.unpack_from("<I", buf)[0]
    if code == TERRAIN_CODE:
        if len(buf) < 8:
            return None
        count = struct.unpack_from("<I", buf, 4)[0]
        if count > 1 << 20:
            raise ChannelError(f"код 28: подозрительное count={count}")
        return 8 + count * 16
    size = FIXED_SIZES.get(code)
    return size


def next_message(buf: bytes) -> tuple[Message, bytes] | None:
    """Достать первое сообщение из буфера; None — данных пока мало."""
    size = message_size(buf)
    if size is None:
        return None
    if len(buf) < size:
        return None
    code = struct.unpack_from("<I", buf)[0]
    return Message(code, buf[4:size]), buf[size:]


def decode_stream(buf: bytes) -> tuple[list[Message], bytes]:
    """Разобрать сколько удастся сообщений; вернуть (сообщения, остаток).

    Неизвестный код (нет в таблице) — останов: остаток возвращается как есть,
    чтобы вызывающий мог залогировать и попробовать восстановиться.
    """
    out: list[Message] = []
    rest = buf
    while rest:
        try:
            got = next_message(rest)
        except ChannelError:
            break
        if got is None:
            # мало данных ИЛИ неизвестный код: если кода нет в таблице — стоп
            if len(rest) >= 4:
                code = struct.unpack_from("<I", rest)[0]
                if code != TERRAIN_CODE and code not in FIXED_SIZES:
                    break
            break
        msg, rest = got
        out.append(msg)
    return out, rest


def extract_world_packet(dump: Path | str) -> bytes:
    """Вытащить пакет мира (65 536 Б, код 2) из frida-дампа.

    Ищем самое длинное входящее (rx) сообщение с кодом 2 — именно так TB
    отвечает на рукопожатие. ``dump`` может быть файлом ИЛИ папкой — тогда
    перебираем все ``*.log`` в ней.
    """
    path = Path(dump)
    if path.is_dir():
        candidates = sorted(path.glob("*.log"))
        if not candidates:
            raise ChannelError(f"в папке {path} нет файлов *.log")
        print(f"ищу пакет мира в {len(candidates)} дампах из {path} …")
        for candidate in candidates:
            try:
                packet = extract_world_packet(candidate)
            except ChannelError:
                continue
            print(f"нашёл мир в {candidate.name}")
            return packet
        raise ChannelError(f"ни в одном дампе {path} нет пакета мира 65 536 Б — "
                           "нужен дамп рабочего сеанса TB <-> Buldozer")
    if not path.is_file():
        folder = path.parent if str(path.parent) else Path(".")
        near = sorted(x.name for x in folder.glob("*.log"))[:12]
        hint = ("рядом лежат: " + ", ".join(near)) if near else             f"в папке {folder} файлов *.log нет"
        raise ChannelError(f"не нашёл дамп: {path} ({hint}).\n"
                           "Подойдёт дамп рабочего сеанса TB <-> Buldozer; можно "
                           "передать и папку целиком — файл выберу сам.")
    best: bytes = b""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            data = rec.get("data") or ""
            if len(data) // 2 <= len(best):
                continue
            raw = bytes.fromhex(data)
            if len(raw) >= 8 and struct.unpack_from("<I", raw)[0] == 2:
                best = raw
    if len(best) < 65536:
        raise ChannelError(f"в дампе {path.name} нет пакета мира 65 536 Б "
                           f"(нашлось {len(best)} Б) — похоже, это не сеанс "
                           "TB <-> Buldozer")
    return best


def world_header(packet: bytes) -> tuple[tuple[int, ...], tuple[float, float]]:
    """Заголовок мира: (код, 28, 128, 128, 1024, 1024) и (40.0, 40.0)."""
    ints = struct.unpack_from("<6I", packet, 0)
    floats = struct.unpack_from("<2f", packet, 24)
    return ints, floats


def build_connect_args(base_args: list[str], token: str) -> list[str]:
    r"""Добавить/заменить ``-connect=pipe\<токен>,8EDB`` в аргументах движка.

    Если параметр уже есть — подставляем свой токен; иначе дописываем в конец.
    """
    marker = "-connect="
    out = [a for a in base_args if not a.lower().startswith(marker)]
    out.append(f"-connect=pipe\\{token},8EDB")
    return out


# ------------------------------------------------------------------ Windows-канал
if sys.platform == "win32":                     # pragma: no cover - только Windows
    _k32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreatePipe.restype = ctypes.c_int
    _k32.CreatePipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_uint32]
    _k32.SetHandleInformation.restype = ctypes.c_int
    _k32.SetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    _k32.ReadFile.restype = ctypes.c_int
    _k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.c_void_p, ctypes.c_void_p]
    _k32.WriteFile.restype = ctypes.c_int
    _k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_void_p]
    _k32.CloseHandle.restype = ctypes.c_int
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.PeekNamedPipe.restype = ctypes.c_int
    _k32.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _k32.GetLastError.restype = ctypes.c_uint32

    HANDLE_FLAG_INHERIT = 0x00000001
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def _hval(handle: object) -> int:
        value = getattr(handle, "value", None)
        return int(value) if value is not None else int(handle)   # pragma: no cover
else:
    _k32 = None
    HANDLE_FLAG_INHERIT = 1
    INVALID_HANDLE_VALUE = -1

    def _hval(handle: object) -> int:            # pragma: no cover
        return 0


class PipeChannel:
    """Два анонимных канала: ``out`` (мы -> движок), ``in`` (движок -> мы).

    ``engine_in``/``engine_out`` — концы, которые получит движок (их надо
    сделать наследуемыми и передать в ``CreateProcess``).
    """

    def __init__(self) -> None:
        if _k32 is None:
            raise ChannelError("named pipes есть только на Windows; запустите "
                               "скрипт на машине с DayZ")
        # _make_pipe возвращает (read, write): мы пишем в write своей трубы,
        # движок читает из read; и наоборот для трубы «движок -> мы».
        self.engine_in, self.out = self._make_pipe()       # движок читает, мы пишем
        self.inbound, self.engine_out = self._make_pipe()  # мы читаем, движок пишет

    @staticmethod
    def _make_pipe() -> tuple[Any, Any]:
        rd = ctypes.c_void_p()
        wr = ctypes.c_void_p()
        if not _k32.CreatePipe(ctypes.byref(rd), ctypes.byref(wr), None, 0):
            raise ChannelError(f"CreatePipe не удался: {_k32.GetLastError()}")
        return rd, wr                            # type: ignore[return-value]

    def mark_inheritable(self) -> None:
        """Сделать наследуемыми только концы движка; наши — нет."""
        for handle in (self.out, self.inbound):
            _k32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0)
        for handle in (self.engine_in, self.engine_out):
            _k32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

    def write(self, data: bytes) -> None:
        written = ctypes.c_uint32(0)
        ok = _k32.WriteFile(self.out, data, len(data), ctypes.byref(written), None)
        if not ok or written.value != len(data):
            raise ChannelError(f"WriteFile: записано {written.value} из {len(data)}, "
                               f"ошибка {_k32.GetLastError()}")

    def read_available(self, chunk: int = 1 << 16) -> bytes:
        """Считать то, что уже лежит в канале (не блокируясь)."""
        ready = ctypes.c_uint32(0)
        if not _k32.PeekNamedPipe(self.inbound, None, 0, None, ctypes.byref(ready), None):
            return b""
        if ready.value == 0:
            return b""
        size = min(chunk, ready.value)
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_uint32(0)
        if not _k32.ReadFile(self.inbound, buf, size, ctypes.byref(got), None):
            return b""
        return buf.raw[: got.value]

    def close(self) -> None:
        for name in ("out", "inbound", "engine_in", "engine_out"):
            handle = getattr(self, name, None)
            if handle and _hval(handle):
                _k32.CloseHandle(handle)
                setattr(self, name, None)


def launch_engine(exe: Path | str, base_args: list[str], channel: PipeChannel,
                  token: str) -> subprocess.Popen:
    """Запустить движок с нашими концами каналов и меткой ``-connect=pipe\\...``."""
    if _k32 is None:                             # pragma: no cover
        raise ChannelError("запуск движка только на Windows")
    channel.mark_inheritable()
    args = build_connect_args(list(base_args), token)
    command = [str(exe), *args]
    print("запускаю:", subprocess.list2cmdline(command))
    return subprocess.Popen(command, cwd=str(Path(exe).parent),
                            close_fds=False, shell=False)


# ------------------------------------------------------------------ probe: сеанс
class DumpWriter:
    """JSON Lines в формате frida_capture (читают analyze_dump/messages)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("w", encoding="utf-8")
        self._t0 = time.monotonic()
        self.count = 0

    def add(self, direction: str, data: bytes, note: str = "") -> None:
        self._fh.write(json.dumps({
            "t": round(time.monotonic() - self._t0, 4),
            "proc": "channel",
            "dir": direction,
            "path": "",
            "handle": 0,
            "data": data.hex(),
            "note": note,
        }, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        self._fh.close()


def run_probe(exe: Path, base_args: list[str], world: bytes, seconds: float,
              out: Path, token: str) -> int:
    """Мы вместо TB: рукопожатие -> мир -> лог всего, что шлёт движок."""
    channel = PipeChannel()
    dump = DumpWriter(out)
    process: subprocess.Popen | None = None
    buffer = b""
    stats: dict[int, int] = {}
    try:
        process = launch_engine(exe, base_args, channel, token)
        print(f"движок запущен (PID {process.pid}); жду рукопожатие [29][5] …")
        deadline = time.monotonic() + seconds
        greeted = False
        while time.monotonic() < deadline:
            chunk = channel.read_available()
            if chunk:
                buffer += chunk
                messages, buffer = decode_stream(buffer)
                for msg in messages:
                    stats[msg.code] = stats.get(msg.code, 0) + 1
                    dump.add("rx" if msg.code != 29 else "rx", encode_message(msg.code, msg.payload),
                             note=f"код {msg.code}")
                    head = " ".join(f"{b:02x}" for b in msg.payload[:12])
                    print(f"  принято: код {msg.code:3d}, {msg.size:6d} Б  {head}")
                    if msg.code == 29 and not greeted:
                        greeted = True
                        channel.write(world)
                        dump.add("tx", world, note="мир 65536")
                        print(f"  отправил мир: {len(world)} Б "
                              f"(заголовок {world_header(world)[0]})")
            elif greeted and stats.get(6, 0) == 0 and time.monotonic() > deadline - seconds + 3:
                pass
            time.sleep(0.005)
        if not greeted:
            print("рукопожатие [29][5] НЕ получено за отведённое время.")
            print("Проверьте: концы каналов (handle_names), флаг -connect= в командной "
                  "строке движка, и не заняты ли профили уже запущенным Buldozer.")
        else:
            print("сеанс завершён. Сообщения по кодам:",
                  {code: n for code, n in sorted(stats.items())})
            if stats.get(6, 0):
                print(f"камера (код 6) пришла {stats[6]} раз — канал РАБОТАЕТ!")
    finally:
        channel.close()
        dump.close()
        print(f"записей в дампе: {dump.count}, файл: {dump.path}")
        if process is not None:
            alive = process.poll() is None
            if alive:
                print("движок ещё работает — закрываю (Ctrl+C не нужен)")
                process.terminate()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Канал Buldozer «мы вместо TB»: анонимные пайпы + протокол")
    parser.add_argument("--version", action="version", version=f"инструменты от {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    extract = sub.add_parser("extract-world", help="вытащить пакет мира из frida-дампа")
    extract.add_argument("--dump", type=Path, required=True)
    extract.add_argument("--out", type=Path, required=True)

    probe = sub.add_parser("probe", help="запустить движок на нашем канале и проверить связь")
    probe.add_argument("--exe", type=Path, required=True, help="DayZ_x64.exe")
    probe.add_argument("--args", default="-buldozer -window -exThreads=0 -mod= "
                                         "-noAsserts -nologs -profiles=P:\\Buldozer "
                                         "-cfg=P:\\Buldozer\\Buldozer.cfg -name=Buldozer",
                       help="параметры движка (как у Terrain Builder)")
    probe.add_argument("--world", type=Path, required=True,
                       help="файл пакета мира (см. extract-world)")
    probe.add_argument("--seconds", type=float, default=20.0)
    probe.add_argument("--token", default="", help="метка канала (по умолчанию своя)")
    probe.add_argument("--out", type=Path, default=Path("channel.log"))

    args = parser.parse_args(argv)
    if args.cmd == "extract-world":
        try:
            packet = extract_world_packet(args.dump)
        except ChannelError as exc:
            print(f"ошибка: {exc}", file=sys.stderr)
            return 2
        args.out.write_bytes(packet)
        ints, floats = world_header(packet)
        print(f"пакет мира: {len(packet)} Б -> {args.out}")
        print(f"заголовок: {ints}, размеры клетки: {floats}")
        return 0
    if args.cmd == "probe":
        if sys.platform != "win32":
            print("probe работает только на Windows (нужны named pipes)", file=sys.stderr)
            return 2
        if not args.exe.is_file():
            print(f"не нашёл exe: {args.exe}", file=sys.stderr)
            return 2
        world = args.world.read_bytes()
        if len(world) != 65536:
            print(f"пакет мира должен быть 65 536 Б, а в файле {len(world)} Б",
                  file=sys.stderr)
            return 2
        token = args.token or f"{random.randint(0x10000, 0xFFFFFF):06X}"
        base = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t
                for t in shlex.split(args.args, posix=False)]
        return run_probe(args.exe, base, world, args.seconds, args.out, token)
    return 2                                        # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
