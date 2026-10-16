"""Вставка поставленных объектов прямо в открытый Terrain Builder (без перезапуска).

Terrain Builder не перечитывает файлы слоёв, пока открыт. Зато он сам импортирует текстовый
файл объектов, если «бросить» его в окно TB (как перетаскивание мышью), — в **активный слой**,
с высотой как **Relative** (формат DayZ Editor: ``"шаблон";X;Z;курс;тангаж;крен;масштаб;высота``).
Импортированные объекты TB сразу передаёт в Buldozer, а при сохранении проекта пишет в слой.

Порядок для каждого поставленного объекта (:class:`LiveImporter`):

1. ждём от скрипта высоту для TB (строка ``Y``; не дольше :data:`WAIT_REL` с);
2. пишем файл ``<папка TB>\\WE_edited_layers\\live\\*.txt`` и отдаём его окну TB
   (сообщение ``WM_DROPFILES``, :class:`Win32Api`) — объект помечается ``tb = "sent"``;
3. скрипт моста (v14, строка ``V``) ищет в Buldozer в той же точке объект той же модели, но не
   наш. Нашёлся — TB объект принял: ``tb = "ok"``, нашу копию скрипт убирает (её место занял
   объект TB); не нашёлся за :data:`VERIFY_TIMEOUT` с — ``tb = "fail"``, наша копия остаётся,
   а объект, как и раньше, попадает в рабочую копию слоя;
4. по желанию (``save``) — после вставок проект TB сохраняется (команда «Сохранить» окну TB),
   и объекты оказываются в файлах слоёв.

Если TB показал окно сообщения (например, «шаблон не найден»), его текст пишется в журнал и
показывается в окне над Buldozer, а новые вставки ждут, пока окно не закроют.
"""
from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import debuglog
from .objects import format_objects

log = debuglog.get_logger("tblive")

TB_EXE = "terrainbuilder.exe"
LIVE_DIR = "live"
WAIT_REL = 2.0             # с: ждать высоту для TB от скрипта (Y|…), потом — по подъёму
VERIFY_TIMEOUT = 12.0      # с: TB не показал объект в Buldozer — вставка не удалась
VERIFY_LATE = 60.0         # с: после неудачи ещё проверяем (TB мог быть занят) — вдруг появился
NO_SCRIPT_REPLY = 4.0      # с: скрипт не отвечает на V|… — он старее v14
RETRY_AFTER = 30.0         # с: после ошибки передачи файла TB — следующая попытка
SAVE_DELAY = 3.0           # с: после последней подтверждённой вставки — сохранить проект TB
KEEP_FILES = 30            # сколько последних файлов вставки хранить
DIALOG_POLL = 0.5          # с: как часто искать окна сообщений TB
DIALOG_WATCH = 90.0        # с: сколько после вставки следить за окнами сообщений TB

WM_DROPFILES = 0x0233
WM_COMMAND = 0x0111
ID_FILE_SAVE = 0xE103      # стандартная команда MFC «Сохранить»
WS_EX_ACCEPTFILES = 0x10


@dataclass
class TbWindow:
    hwnd: int
    pid: int
    cls: str
    title: str
    accepts: bool = False          # WS_EX_ACCEPTFILES — принимает WM_DROPFILES
    ole: bool = False              # зарегистрирован OLE drop target (перетаскивание мышью)
    top: bool = False              # окно верхнего уровня
    area: int = 0

    def describe(self) -> str:
        flags = ("A" if self.accepts else "-") + ("O" if self.ole else "-") + ("T" if self.top else "-")
        return f"{self.hwnd:#x} {flags} {self.cls!r} {self.title[:40]!r}"


class TbApi:
    """Что нужно от Windows (в тестах — подделка)."""

    def pids(self) -> set[int]:
        return set()

    def windows(self) -> list[TbWindow]:
        return []

    def drop(self, win: TbWindow, paths: list[str]) -> str:
        return "не Windows"

    def dialogs(self, pids: Iterable[int]) -> dict[int, str]:
        return {}

    def save(self, win: TbWindow) -> str:
        return "не Windows"


def drop_target(wins: list[TbWindow]) -> TbWindow | None:
    """Окно TB, которому отдавать файл: как при перетаскивании мышью на карту — самое большое
    дочернее окно, принимающее WM_DROPFILES (вид карты); если такого нет — главное окно."""
    accepting = [w for w in wins if w.accepts]
    if not accepting:
        return None
    return sorted(accepting, key=lambda w: (w.top, -w.area))[0]


def main_window(wins: list[TbWindow]) -> TbWindow | None:
    tops = [w for w in wins if w.top]
    return max(tops, key=lambda w: w.area) if tops else None


def dropfiles_blob(paths: list[str], point: tuple[int, int] = (10, 10)) -> bytes:
    """Структура DROPFILES (широкие строки) + список путей, как её кладёт Проводник."""
    head = struct.pack("<IiiII", 20, point[0], point[1], 0, 1)
    body = "".join(p + "\0" for p in paths) + "\0"
    return head + body.encode("utf-16-le")


@dataclass
class LiveStatus:
    sent: int = 0
    ok: int = 0
    failed: int = 0
    waiting: int = 0
    message: str = ""


@dataclass
class LiveImporter:
    """Очередь вставки объектов сеанса в открытый TB. Вызывать :meth:`step` на каждом такте."""

    api: TbApi
    folder: Callable[[], Path | None]          # папка TB проекта (куда класть файлы вставки)
    clock: Callable[[], float] = time.time
    enabled: bool = True
    save: bool = False
    message: str = ""                          # последнее, что сказать пользователю
    skip: set = field(default_factory=set)     # id объектов из прошлого сеанса — сами не шлём
    _seen: dict[int, float] = field(default_factory=dict)
    _blocked_until: float = 0.0
    _dialogs: dict[int, str] = field(default_factory=dict)
    _dialog_at: float = 0.0
    _last_drop: float = -1e9
    _save_due: float = 0.0
    _logged_windows: tuple = ()
    _counter: int = 0

    # ---- что послать скрипту
    def verify_lines(self, objects: Iterable) -> list[str]:
        now = self.clock()
        out = []
        for o in objects:
            if o.tb == "sent" or (o.tb == "fail" and now - o.tb_at < VERIFY_LATE):
                model = o.model.replace("|", "")
                out.append(f"V|{o.id}|{o.x:.3f}|{o.z:.3f}|{model}")
        return out

    # ---- такт
    def step(self, objects: list, state, names: Callable[[], dict[str, str] | None],
             offset: Callable[[], tuple[float, float] | None],
             template_of: Callable[[str], str | None]) -> bool:
        """Продвинуть очередь. ``offset()`` — начало рамки карты TB (из проекта), ``names()`` —
        {ключ модели: имя шаблона TB}; оба зовутся только при отправке. True — у объектов поменялось состояние вставки (обновить окно и команды
        скрипта)."""
        now = self.clock()
        changed = self._check_replies(objects, state, now)
        if not self.enabled:
            return changed
        for o in objects:
            self._seen.setdefault(o.id, now)
        changed |= self._watch_dialogs(now)
        if self._dialogs or now < self._blocked_until:
            return changed
        ready = [o for o in objects if not o.tb and not o.failed and o.id not in self.skip
                 and (o.tb_rel is not None or now - self._seen.get(o.id, now) >= WAIT_REL)]
        if ready:
            changed |= self._send(ready, names, offset, template_of, now)
        if self.save and self._save_due and now >= self._save_due:
            self._save_due = 0.0
            self._save_project()
        return changed

    def _check_replies(self, objects: list, state, now: float) -> bool:
        changed = False
        verified = getattr(state, "verified", {}) if state is not None else {}
        for o in objects:
            if o.tb not in ("sent", "fail"):
                continue
            n = verified.get(o.id)
            if n:
                was = o.tb
                o.tb = "ok"
                changed = True
                self._save_due = now + SAVE_DELAY
                log.info("tb live: object #%d %s shown by Terrain Builder in Buldozer%s", o.id,
                         o.model, " (late)" if was == "fail" else "")
                continue
            if o.tb == "sent" and n is None and now - o.tb_at > NO_SCRIPT_REPLY and state is not None \
                    and not verified:
                if not self.message.startswith("Скрипт моста"):
                    self.message = ("Скрипт моста не проверяет вставку (нужна версия 14): обновите "
                                    "его в стартовом окне и нажмите F10 в Buldozer.")
                    log.warning("tb live: script does not answer V| lines (bridge older than v14)")
            if o.tb == "sent" and now - o.tb_at > VERIFY_TIMEOUT:
                o.tb = "fail"
                changed = True
                self.message = ("Terrain Builder не показал вставленный объект в Buldozer — он "
                                "остаётся нашим и записывается в рабочую копию слоя.")
                log.warning("tb live: object #%d %s not seen in Buldozer %.0f s after the drop",
                            o.id, o.model, VERIFY_TIMEOUT)
        return changed

    def _watch_dialogs(self, now: float) -> bool:
        if not self._dialogs and now - self._last_drop > DIALOG_WATCH:
            return False
        if now - self._dialog_at < DIALOG_POLL:
            return False
        self._dialog_at = now
        pids = self.api.pids()
        found = self.api.dialogs(pids) if pids else {}
        for hwnd, text in found.items():
            if hwnd not in self._dialogs:
                log.warning("tb live: Terrain Builder shows a dialog %#x: %s", hwnd, text)
                self.message = f"Terrain Builder показал окно (вставка ждёт, пока его закроют): {text}"
        closed = set(self._dialogs) - set(found)
        if closed and not found:
            log.info("tb live: Terrain Builder dialog closed")
            if self.message.startswith("Terrain Builder показал окно"):
                self.message = ""
        self._dialogs = found
        return False

    def _send(self, ready: list, names, offset, template_of, now: float) -> bool:
        wins = self.api.windows()
        if not wins:
            self._blocked_until = now + 2.0    # TB не запущен — объекты идут только в рабочую копию
            return False
        sig = tuple(sorted(w.describe() for w in wins))
        if sig != self._logged_windows:
            self._logged_windows = sig
            log.info("tb live: Terrain Builder windows: %s", "; ".join(sig))
        target = drop_target(wins)
        changed = False
        usable = []
        for o in ready:
            if template_of(o.model) is None:
                o.tb, o.tb_at = "fail", now      # TB не знает модель — импорт бы оборвался
                changed = True
                log.info("tb live: object #%d %s has no Terrain Builder template — not sent", o.id, o.model)
            else:
                usable.append(o)
        if not usable:
            return changed
        if target is None:
            ole = any(w.ole for w in wins)
            self.message = ("Terrain Builder не принимает файлы сообщением WM_DROPFILES" +
                            (" (только перетаскиванием мышью)" if ole else "") +
                            " — объекты пишутся в рабочую копию слоя.")
            log.warning("tb live: no Terrain Builder window accepts dropped files (ole=%s)", ole)
            self._blocked_until = now + RETRY_AFTER
            return changed
        folder = self.folder()
        if folder is None:
            self.message = "Не найдена папка проекта TB — некуда положить файл для вставки."
            self._blocked_until = now + RETRY_AFTER
            return changed
        live = Path(folder) / "WE_edited_layers" / LIVE_DIR
        self._counter += 1
        path = live / time.strftime(f"we_%Y%m%d_%H%M%S_{self._counter}.txt", time.localtime(now))
        origin = offset()
        text = format_objects((o.tb_object(True, True, names()) for o in usable), origin)
        try:
            live.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")
        except OSError as exc:
            self.message = f"Не удалось записать файл для вставки в TB: {exc}"
            self._blocked_until = now + RETRY_AFTER
            return changed
        self._prune(live)
        err = self.api.drop(target, [str(path)])
        if err:
            self.message = f"Не удалось передать файл Terrain Builder: {err}"
            log.warning("tb live: drop to %s failed: %s", target.describe(), err)
            self._blocked_until = now + RETRY_AFTER
            return changed
        for o in usable:
            o.tb, o.tb_at = "sent", now
        self._last_drop = now
        log.info("tb live: %d object(s) dropped to Terrain Builder %s: %s (frame origin %s)",
                 len(usable), target.describe(), path, origin)
        self.message = ""
        return True

    def _save_project(self) -> None:
        main = main_window(self.api.windows())
        if main is None:
            return
        err = self.api.save(main)
        if err:
            self.message = f"Проект TB не сохранён автоматически: {err} — сохраните его в TB (Ctrl+S)."
            log.warning("tb live: save command not sent: %s", err)
        else:
            log.info("tb live: save command sent to Terrain Builder %s", main.describe())

    @staticmethod
    def _prune(folder: Path) -> None:
        try:
            files = sorted(folder.glob("we_*.txt"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for p in files[:-KEEP_FILES]:
            try:
                p.unlink()
            except OSError:
                pass

    def status(self, objects: Iterable) -> LiveStatus:
        st = LiveStatus(message=self.message)
        for o in objects:
            if o.tb == "ok":
                st.ok += 1
            elif o.tb == "sent":
                st.sent += 1
            elif o.tb == "fail":
                st.failed += 1
            elif not o.failed:
                st.waiting += 1
        return st


# ----------------------------------------------------------------------- Windows

class Win32Api(TbApi):                                   # pragma: no cover - нужен Windows
    """Окна процесса TerrainBuilder.exe, передача файла (WM_DROPFILES), окна сообщений TB."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as wt
        self.ct, self.wt = ctypes, wt
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u32, self.k32 = u32, k32
        self.ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        u32.EnumWindows.argtypes = [self.ENUMPROC, wt.LPARAM]
        u32.EnumChildWindows.argtypes = [wt.HWND, self.ENUMPROC, wt.LPARAM]
        u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u32.GetWindowThreadProcessId.restype = wt.DWORD
        u32.IsWindowVisible.argtypes = [wt.HWND]
        u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        u32.GetWindowLongW.restype = wt.LONG
        u32.GetPropW.argtypes = [wt.HWND, wt.LPCWSTR]
        u32.GetPropW.restype = wt.HANDLE
        u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u32.PostMessageW.restype = wt.BOOL
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.OpenProcess.restype = wt.HANDLE
        k32.VirtualAllocEx.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_size_t, wt.DWORD, wt.DWORD]
        k32.VirtualAllocEx.restype = wt.LPVOID
        k32.WriteProcessMemory.argtypes = [wt.HANDLE, wt.LPVOID, wt.LPCVOID, ctypes.c_size_t,
                                           ctypes.POINTER(ctypes.c_size_t)]
        k32.WriteProcessMemory.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wt.HANDLE

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                        ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD),
                        ("szExeFile", wt.WCHAR * 260)]
        self.PE = PROCESSENTRY32W
        k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

    # -- процессы и окна
    def pids(self) -> set[int]:
        ct = self.ct
        snap = self.k32.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == ct.c_void_p(-1).value:
            return set()
        out = set()
        try:
            pe = self.PE()
            pe.dwSize = ct.sizeof(self.PE)
            ok = self.k32.Process32FirstW(snap, ct.byref(pe))
            while ok:
                if pe.szExeFile.lower() == TB_EXE:
                    out.add(int(pe.th32ProcessID))
                ok = self.k32.Process32NextW(snap, ct.byref(pe))
        finally:
            self.k32.CloseHandle(snap)
        return out

    def _text(self, fn, hwnd) -> str:
        buf = self.ct.create_unicode_buffer(512)
        fn(hwnd, buf, 512)
        return buf.value

    def _info(self, hwnd, pid: int, top: bool) -> TbWindow:
        u32, wt = self.u32, self.wt
        rect = wt.RECT()
        u32.GetWindowRect(hwnd, self.ct.byref(rect))
        ex = u32.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
        return TbWindow(int(hwnd), pid, self._text(u32.GetClassNameW, hwnd),
                        self._text(u32.GetWindowTextW, hwnd), bool(ex & WS_EX_ACCEPTFILES),
                        bool(u32.GetPropW(hwnd, "OleDropTargetInterface")), top,
                        max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top))

    def _tops(self, pids: set[int]) -> list[tuple[int, int]]:
        u32, wt, ct = self.u32, self.wt, self.ct
        tops: list[tuple[int, int]] = []

        def cb(hwnd, _lp):
            pid = wt.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ct.byref(pid))
            if pid.value in pids and u32.IsWindowVisible(hwnd):
                tops.append((hwnd, int(pid.value)))
            return True
        u32.EnumWindows(self.ENUMPROC(cb), 0)
        return tops

    def windows(self) -> list[TbWindow]:
        pids = self.pids()
        if not pids:
            return []
        out: list[TbWindow] = []
        for hwnd, pid in self._tops(pids):
            out.append(self._info(hwnd, pid, True))
            kids: list[int] = []

            def cb(child, _lp):
                if self.u32.IsWindowVisible(child):
                    kids.append(child)
                return True
            self.u32.EnumChildWindows(hwnd, self.ENUMPROC(cb), 0)
            out += [self._info(k, pid, False) for k in kids[:400]]
        return out

    def dialogs(self, pids: Iterable[int]) -> dict[int, str]:
        pids = set(pids)
        found: dict[int, str] = {}
        for hwnd, _pid in self._tops(pids):
            if self._text(self.u32.GetClassNameW, hwnd) != "#32770":
                continue
            parts = [self._text(self.u32.GetWindowTextW, hwnd)]

            def cb(child, _lp):
                if self._text(self.u32.GetClassNameW, child) == "Static":
                    t = self._text(self.u32.GetWindowTextW, child).strip()
                    if t:
                        parts.append(t)
                return True
            self.u32.EnumChildWindows(hwnd, self.ENUMPROC(cb), 0)
            found[int(hwnd)] = " | ".join(p for p in parts if p)
        return found

    # -- передать файл
    def drop(self, win: TbWindow, paths: list[str]) -> str:
        ct = self.ct
        blob = dropfiles_blob(paths)
        proc = self.k32.OpenProcess(0x0008 | 0x0020 | 0x0400, False, win.pid)
        if not proc:
            err = ct.get_last_error()
            if err == 5:
                return ("нет доступа к процессу Terrain Builder (он запущен от администратора? "
                        "запустите и эту программу от администратора)")
            return f"OpenProcess: ошибка {err}"
        try:
            mem = self.k32.VirtualAllocEx(proc, None, len(blob), 0x3000, 0x04)
            if not mem:
                return f"VirtualAllocEx: ошибка {ct.get_last_error()}"
            done = ct.c_size_t()
            if not self.k32.WriteProcessMemory(proc, mem, blob, len(blob), ct.byref(done)):
                return f"WriteProcessMemory: ошибка {ct.get_last_error()}"
            # память не освобождаем: TB прочитает её, когда обработает сообщение (сотни байт)
            if not self.u32.PostMessageW(win.hwnd, WM_DROPFILES, mem, 0):
                err = ct.get_last_error()
                if err == 5:
                    return "Windows не пускает сообщение в окно TB (TB запущен от администратора?)"
                return f"PostMessage: ошибка {err}"
        finally:
            self.k32.CloseHandle(proc)
        return ""

    def save(self, win: TbWindow) -> str:
        if not win.cls.startswith("Afx"):
            return f"окно TB не MFC ({win.cls}) — команду «Сохранить» не знаем"
        if not self.u32.PostMessageW(win.hwnd, WM_COMMAND, ID_FILE_SAVE, 0):
            return f"PostMessage: ошибка {self.ct.get_last_error()}"
        return ""


def default_api() -> TbApi:
    if os.name != "nt":
        return TbApi()
    try:                                                 # pragma: no cover - нужен Windows
        return Win32Api()
    except (OSError, AttributeError) as exc:             # pragma: no cover
        log.warning("tb live: Win32 API not available: %s", exc)
        return TbApi()
