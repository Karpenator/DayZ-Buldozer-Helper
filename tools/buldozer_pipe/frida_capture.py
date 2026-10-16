"""Перехват реального трафика канала Terrain Builder <-> Buldozer (Frida).

Канал — это Windows named pipe, имя которого TB передаёт движку параметром
``-connect=pipe\\<токен>,8EDB``. Содержимое канала нигде не документировано, поэтому
единственный способ узнать протокол — записать то, чем TB и Buldozer обмениваются,
и разобрать запись.

Что делает скрипт:

1. подключается (или запускает) к двум процессам — Terrain Builder и Buldozer;
2. ставит в них ``capture.js``: перехват ``ReadFile``/``WriteFile`` и открытий каналов;
3. пишет всё, что прошло по каналу, в JSON Lines — по файлу на процесс.

Запуск (нужен ``pip install frida-tools``)::

    # найти имена процессов
    python frida_capture.py --list

    # обычный сценарий: TB и Buldozer уже запущены, жмём «Connect to Buldozer»,
    # правим рельеф в TB и смотрим, что летит по каналу
    python frida_capture.py --attach TerrainBuilder.exe --attach DayZ_x64.exe

    # если удобнее запускать движок из скрипта (без BattlEye — только DayZDiag!)
    python frida_capture.py --spawn "E:\\...\\DayZDiag_x64.exe" --args "-buldozer -window ..."

Авторский дамп потом разбирается::

    python analyze_dump.py capture_*.log --show 5

Ограничения и предупреждения:

* перехват идёт на уровне WinAPI, поэтому нужны права, достаточные для отладки
  чужого процесса (обычно тот же пользователь; иногда — «Запуск от имени
  администратора»);
* **не подключайтесь Frida к розничному DayZ_x64.exe с BattlEye**, если играете на
  серверах: вмешательство в процесс может быть воспринято как чит. Для опытов
  используйте DayZDiag_x64.exe (buldozer-режим под ним работает так же);
* TB пишет много обычных файлов — они отфильтрованы (берём только `FILE_TYPE_PIPE`).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["CaptureStats", "record_from_message", "main", "AGENT_PATH",
           "parse_tasklist_csv", "resolve_target", "windows_processes",
           "TOOL_VERSION", "tool_banner"]

AGENT_PATH = Path(__file__).with_name("capture.js")

# Метка свежести копии инструментов: печатается в --list и --watch. Пользователь
# сверяет её с датой архива (см. README «Обновить копию из архива без git»).
# При обновлении файлов обновите и дату — она одна и та же и у probe_pipe.py.
TOOL_VERSION = "2026-10-12"


def tool_banner() -> str:
    """Строка о дате сборки — для --list/--watch и быстрой проверки копии."""
    return f"инструменты от {TOOL_VERSION}"


# Имена процессов, которые нам интересны: их показываем первыми в любом списке.
OUR_KEYWORDS = ("dayz", "terrain", "buldozer", "steam", "tools")


class CaptureError(RuntimeError):
    """Понятная ошибка (нет frida, нет процесса, нет прав)."""


@dataclass
class CaptureStats:
    """Счётчики по одному перехваченному процессу."""

    proc: str
    path: Path
    rx: int = 0
    tx: int = 0
    events: int = 0
    bytes: int = 0
    pipes: set[str] = field(default_factory=set)
    elapsed_from: float = 0.0      # момент старта записи (для отметок времени)

    def summary(self) -> str:
        return (f"{self.proc}: записей {self.events} (rx {self.rx}, tx {self.tx}), "
                f"{self.bytes} Б, каналов: {len(self.pipes)}")


def record_from_message(payload: dict[str, Any], data: bytes | None, elapsed: float,
                        proc: str) -> dict[str, Any] | None:
    """Превратить сообщение агента в запись дампа (или None, если это не трафик)."""
    kind = str(payload.get("kind") or "")
    if kind in ("tx", "rx"):
        direction = kind
    elif kind in ("open", "create", "connect", "name"):
        direction = "open"
    else:
        return None
    path = str(payload.get("path") or payload.get("name") or "")
    note = str(payload.get("api") or kind)
    for key in ("ok", "completed", "requested", "truncated_to"):
        if key in payload:
            note += f" {key}={payload[key]}"
    return {
        "t": round(elapsed, 4),
        "proc": proc,
        "dir": direction,
        "path": path,
        "handle": int(payload.get("handle") or 0),
        "data": data.hex() if data else "",
        "note": note,
    }


def _missing_frida_hint() -> str:
    """Что делать, если frida стоит, но в другом Python (частая история с py -3)."""
    return ("frida не установлен для ЭТОГО Python.\n"
            f"  интерпретатор: {sys.executable}\n"
            f"  версия Python: {sys.version.split()[0]}\n"
            "  поставьте: python -m pip install frida-tools\n"
            "  если frida уже стоит в виртуальном окружении (например .venv-build), "
            "запускайте\n  скрипт тем же питоном: "
            ".venv-build\\Scripts\\python.exe frida_capture.py ...\n"
            "  в PowerShell команда 'py -3' берёт системный Python и виртуальное "
            "окружение не видит.")


def _import_frida():
    try:
        import frida                                  # type: ignore[import-not-found]
    except ImportError as exc:                        # pragma: no cover - зависит от среды
        raise CaptureError(_missing_frida_hint()) from exc
    return frida


def parse_tasklist_csv(text: str) -> list[tuple[int, str]]:
    """Разобрать вывод ``tasklist /FO CSV /NH`` в список (pid, имя).

    Своя Windows видит и те процессы, которые Frida не показывает (запущенные от
    администратора / защищённые), поэтому tasklist — наш источник PID.
    """
    found: list[tuple[int, str]] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        name = (row[0] or "").strip()
        pid_text = (row[1] or "").strip().replace("\u00a0", "").replace(" ", "")
        if name and pid_text.isdigit():
            found.append((int(pid_text), name))
    return found


def windows_processes() -> list[tuple[int, str]]:
    """Процессы глазами Windows (tasklist). Пусто не на Windows или при ошибке."""
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):        # pragma: no cover - Windows
        return []
    return parse_tasklist_csv(result.stdout)


def _is_elevated() -> bool:
    """Запущена ли консоль от администратора: без этого часть процессов не видна."""
    if sys.platform != "win32":
        return False
    try:                                                # pragma: no cover - Windows
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                   # pragma: no cover
        return False


def _match(procs: list[tuple[int, str]], needle: str) -> tuple[tuple[int, str] | None, bool]:
    """Найти процесс по точному имени, иначе по подстроке: ((pid, имя) | None, точное?)"""
    lowered = needle.lower()
    for pid, name in procs:
        if name.lower() == lowered:
            return (pid, name), True
    for pid, name in procs:
        if lowered in name.lower():
            return (pid, name), False
    return None, False


def resolve_target(spec: str, frida_procs: list[tuple[int, str]],
                   win_procs: list[tuple[int, str]]) -> tuple[int, str]:
    """К чему подключаться: (PID, пояснение).

    Умеет: PID напрямую, имя процесса, часть имени. Если Frida процесса не видит, но
    Windows видит (частая история для процессов «от администратора»), берём PID у Windows.
    """
    spec = spec.strip()
    if spec.isdigit():
        return int(spec), "PID указан вручную"
    for source, label in ((frida_procs, "Frida"), (win_procs, "Windows")):
        (found, exact) = _match(source, spec)
        if found is None:
            continue
        pid, name = found
        how = "точное имя" if exact else f"имя содержит {spec!r}"
        if label == "Windows":
            return pid, (f"{name}, PID {pid} — Frida его не видит (обычно процесс запущен "
                         f"от администратора), подключаюсь по PID")
        return pid, f"{name}, PID {pid} ({how})"
    raise CaptureError(
        f"процесс {spec!r} не найден ни Frida, ни Windows.\n"
        "  * запущено ли приложение (Terrain Builder и/или DayZ в режиме Buldozer)?\n"
        "  * подсказка: `python frida_capture.py --list` покажет, что видят оба списка;\n"
        "  * можно подключиться по номеру: --attach 12345 (PID из диспетчера задач);\n"
        "  * одна из причин — процесс запущен от администратора, а консоль нет: "
        "запустите её «от имени администратора».")


def _name_for(pid: int, *sources: list[tuple[int, str]]) -> str:
    for source in sources:
        for proc_pid, name in source:
            if proc_pid == pid:
                return name
    return f"PID {pid}"


def order_hidden(missing: list[tuple[int, str]], limit: int = 80,
                 keywords: tuple[str, ...] = OUR_KEYWORDS
                 ) -> tuple[list[tuple[int, str]], int, int]:
    """Порядок показа «Frida не видит»: сначала наши процессы, потом остальные.

    Возвращает (что показываем, сколько всего скрыто, сколько не влезло).
    """
    def ours(item: tuple[int, str]) -> bool:
        name = item[1].lower()
        return any(word in name for word in keywords)

    first = [item for item in missing if ours(item)]
    rest = [item for item in missing if not ours(item)]
    room = max(0, limit - len(first))
    shown = first + rest[:room]
    return shown, len(missing), max(0, len(rest) - room)


def _list_processes(find: str = "") -> int:
    """Показать, что видят Frida и Windows, и чем они отличаются."""
    frida = _import_frida()
    print(f"frida {getattr(frida, '__version__', '?')} из {sys.executable}")
    print("консоль от администратора: " + ("да" if _is_elevated()
          else "НЕТ — процессы «от администратора» могут быть не видны"))
    device = frida.get_local_device()
    procs = sorted(((p.pid, p.name) for p in device.enumerate_processes()),
                   key=lambda item: item[1].lower())
    needle = find.strip().lower()
    shown = [item for item in procs if not needle or needle in item[1].lower()]
    print(f"\nFrida видит процессов: {len(shown)}"
          + (f" (из {len(procs)}, фильтр {find!r})" if needle else ""))
    for pid, name in shown:
        print(f"      {pid:6d}  {name}")

    win = windows_processes()
    if not win:
        if sys.platform != "win32":
            print("\n(список Windows доступен только на Windows)")
        return 0
    visible = {pid for pid, _ in procs}
    missing = sorted(((pid, name) for pid, name in win if pid not in visible),
                     key=lambda item: item[1].lower())
    missing = [item for item in missing if not needle or needle in item[1].lower()]
    if not missing:
        print("\nWindows не видит ничего сверх списка Frida.")
        return 0
    shown, total, left = order_hidden(missing)
    print(f"\nWindows видит, а Frida — нет ({total} шт.; наши — первыми):")
    for pid, name in shown:
        mark = "   <-- похоже на наше" if any(word in name.lower() for word in OUR_KEYWORDS) else ""
        print(f"      {pid:6d}  {name}{mark}")
        if mark and not _is_elevated():
            print("               ^ запущен от администратора: консоль его не увидит и не "
                  "подключится")
    if left:
        print(f"      … ещё {left} процессов (полный список: --list --find <часть имени>)")
    if not _is_elevated():
        print("\n  «Консоль от администратора: НЕТ» — поэтому столько процессов и скрыто.\n"
              "  Что делать: запустить PowerShell «от имени администратора» и повторить "
              "--list;\n  либо подключаться к скрытым по PID: "
              "python frida_capture.py --attach <PID>")
    else:
        print("\n  подключаться к скрытым можно по PID: "
              "python frida_capture.py --attach <PID>")
    return 0


class Capture:
    """Один перехваченный процесс: сессия Frida, скрипт-агент, дамп и счётчики."""

    def __init__(self, name: str, pid: int, session, handle, script, stat: CaptureStats,
                 out_path: Path) -> None:
        self.name = name
        self.pid = pid
        self.session = session
        self.handle = handle
        self.script = script
        self.stat = stat
        self.out_path = out_path

    def stop(self) -> None:
        try:
            self.script.unload()
        except Exception:
            pass
        try:
            self.handle.close()
        except Exception:
            pass
        try:
            self.session.detach()
        except Exception:
            pass


def _make_on_message(name: str, stat: CaptureStats, handle):
    """Обработчик сообщений агента: пишем записи в дамп и печатаем сводку."""
    def on_message(message, data, stat=stat, handle=handle, name=name):
        if message.get("type") != "send":
            if message.get("type") == "error":
                print(f"[{name}] ошибка агента: {message.get('description')}", file=sys.stderr)
            return
        payload = message.get("payload") or {}
        if payload.get("kind") == "warn":
            print(f"[{name}] {payload.get('text')}")
            return
        record = record_from_message(payload, data, time.monotonic() - stat.elapsed_from,
                                     name)
        if record is None:
            return
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        stat.events += 1
        stat.bytes += len(record["data"]) // 2
        if record["dir"] == "tx":
            stat.tx += 1
        elif record["dir"] == "rx":
            stat.rx += 1
        if record["path"]:
            if record["path"] not in stat.pipes:
                stat.pipes.add(record["path"])
                print(f"[{name}] канал: {record['path']} ({record['note']})")
        if record["dir"] != "open" and stat.events % 20 == 0:
            print(f"[{name}] {stat.summary()}")
    return on_message


def _start_capture(device, source: str, args: argparse.Namespace, name: str, pid: int,
                   session, stamp: str, started: float) -> Capture:
    """Загрузить агент в сессию и завести дамп для процесса."""
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
    out_path = args.out / f"capture_{safe}_{stamp}.log"
    stat = CaptureStats(proc=name, path=out_path)
    stat.elapsed_from = started                                  # type: ignore[attr-defined]
    handle = out_path.open("w", encoding="utf-8")
    script = session.create_script(source)
    script.on("message", _make_on_message(name, stat, handle))
    script.load()
    script.exports_sync.configure({                              # type: ignore[attr-defined]
        "nt": bool(args.nt),
        "hexLimit": int(args.hex_limit),
        "opensOnly": bool(args.opens_only),
        "resolveNames": not bool(args.no_resolve_names),
    })
    hello = script.exports_sync.hello()                          # type: ignore[attr-defined]
    print(f"[{name}] перехват включён (pid {hello['pid']}, {hello['arch']}) -> {out_path}")
    return Capture(name, pid, session, handle, script, stat, out_path)


def _attach_spec(device, spec: str, args: argparse.Namespace, started: float, stamp: str
                 ) -> Capture:
    """Подключиться к процессу по имени/PID и сразу включить перехват."""
    frida_procs = [(proc.pid, proc.name) for proc in device.enumerate_processes()]
    win_procs = windows_processes()
    target, why = resolve_target(spec, frida_procs, win_procs)
    try:
        session = device.attach(target)
    except Exception as exc:
        raise CaptureError(
            f"не смог подключиться к {spec!r} ({why}): {exc}\n"
            "  * «access denied» / «unable to inject» — запустите консоль "
            "«от имени администратора»;\n"
            "  * для DayZ берите DayZDiag_x64.exe: розничный exe защищён BattlEye, "
            "к нему Frida не подключится.") from exc
    name = _name_for(target, frida_procs, win_procs)
    print(f"подключился: {name} (PID {target}) — {why}")
    source = AGENT_PATH.read_text(encoding="utf-8")
    return _start_capture(device, source, args, name, target, session, stamp, started)


def _watch_targets(args: argparse.Namespace, source: str, known_pids: set[int],
                   started: float, stamp: str) -> list[Capture]:
    """Подцепиться к процессам, которых раньше не было (TB перезапустил Buldozer)."""
    device = None
    try:
        device = _import_frida().get_local_device()
    except CaptureError:
        return []
    procs = [(proc.pid, proc.name) for proc in device.enumerate_processes()]
    procs += [item for item in windows_processes() if item not in procs]
    found: list[Capture] = []
    for pattern in args.watch:
        needle = pattern.lower()
        for pid, name in sorted(procs):
            if pid in known_pids or needle not in name.lower():
                continue
            known_pids.add(pid)
            try:
                session = device.attach(pid)
                print(f"появился {name} (PID {pid}) — подключаюсь")
                found.append(_start_capture(device, source, args, name, pid, session,
                                            stamp, started))
            except Exception as exc:                    # процесс мог уже закрыться
                print(f"[{name}] не подключился: {exc}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перехват трафика канала TB <-> Buldozer")
    parser.add_argument("--attach", action="append", default=[],
                        help="имя или pid процесса (можно повторять)")
    parser.add_argument("--watch", action="append", default=[],
                        help="ждать появления процесса с таким именем (например --watch dayz): "
                             "нужно, когда Terrain Builder сам запускает Buldozer")
    parser.add_argument("--watch-timeout", type=float, default=0.0,
                        help="сколько секунд ждать появления (0 — до Ctrl+C)")
    parser.add_argument("--spawn", default="", help="exe для запуска (например DayZDiag_x64.exe)")
    parser.add_argument("--args", default="", help="параметры для --spawn (одна строка)")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="сколько секунд писать (0 — до Ctrl+C)")
    parser.add_argument("--out", type=Path, default=Path("."), help="куда писать дампы")
    parser.add_argument("--hex-limit", type=int, default=65536,
                        help="обрезать одну запись до N байт (0 — не обрезать)")
    parser.add_argument("--nt", action="store_true",
                        help="дополнительно перехватывать ntdll!NtReadFile/NtWriteFile")
    parser.add_argument("--opens-only", action="store_true",
                        help="только открытия каналов, без содержимого")
    parser.add_argument("--no-resolve-names", action="store_true",
                        help="не выяснять имена уже открытых каналов (NtQueryObject)")
    parser.add_argument("--list", action="store_true", help="показать процессы и выйти")
    parser.add_argument("--find", default="",
                        help="с --list: показать только процессы, содержащие эту подстроку")
    args = parser.parse_args(argv)
    print(tool_banner())

    if args.list:
        try:
            return _list_processes(args.find)
        except CaptureError as exc:
            print(f"ошибка: {exc}", file=sys.stderr)
            return 2

    try:
        frida = _import_frida()
    except CaptureError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    if not AGENT_PATH.is_file():
        print(f"ошибка: нет файла агента {AGENT_PATH}", file=sys.stderr)
        return 2
    print(f"frida {getattr(frida, '__version__', '?')} из {sys.executable}")

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    started = time.monotonic()
    captures: list[Capture] = []
    known_pids: set[int] = set()
    device = frida.get_local_device()

    try:
        for spec in args.attach:
            capture = _attach_spec(device, spec, args, started, stamp)
            captures.append(capture)
            known_pids.add(capture.pid)
        if args.spawn:
            argv_spawn = [args.spawn, *([args.args] if args.args else [])]
            try:
                pid = device.spawn(argv_spawn)
            except Exception as exc:
                raise CaptureError(f"spawn не удался ({' '.join(argv_spawn)}): {exc}") from exc
            session = device.attach(pid)
            print(f"запущен и перехвачен: {Path(args.spawn).name} (PID {pid})")
            captures.append(_start_capture(device, AGENT_PATH.read_text(encoding="utf-8"),
                                           args, Path(args.spawn).name, pid, session,
                                           stamp, started))
            known_pids.add(pid)
            device.resume(pid)
        if not captures and not args.watch:
            raise CaptureError("укажите --attach <имя или PID>, --watch <имя> или --spawn <exe>")
    except CaptureError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    source = AGENT_PATH.read_text(encoding="utf-8")
    if args.watch:
        print(f"жду появления: {', '.join(args.watch)}"
              + (f" (не дольше {args.watch_timeout:g} с)" if args.watch_timeout else ""))
    print("жмите «Connect to Buldozer» в TB, правьте рельеф, перемещайте объекты…")
    print("Ctrl+C — остановить запись")

    watch_deadline = (started + args.watch_timeout) if args.watch_timeout else None
    deadline = (started + args.seconds) if args.seconds else None
    last_heartbeat = started
    try:
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            if args.watch and (watch_deadline is None or now < watch_deadline):
                captures.extend(_watch_targets(args, source, known_pids, started, stamp))
                if now - last_heartbeat > 15 and watch_deadline:
                    left = max(0.0, watch_deadline - now)
                    print(f"… жду ещё {left:.0f} с: {', '.join(args.watch)}")
                    last_heartbeat = now
            elif not args.watch and deadline is None:
                pass
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nостановлено пользователем")
    finally:
        for capture in captures:
            capture.stop()

    print()
    for capture in captures:
        print(capture.stat.summary())
    if captures:
        paths = " ".join(str(c.out_path) for c in captures)
        print(f"\nразбор: python analyze_dump.py {paths}")
        print("пришлите программе эти файлы (или отчёт analyze_dump.py) — по ним соберём канал")
    else:
        print("ни одного процесса не перехвачено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
