"""Окно Buldozer и клавиши для окна поверх Buldozer (без Qt).

* ``parse_hotkey("Shift+F9")`` — разбор сочетания из настроек (работает везде, тестируется);
* остальное — тонкие обёртки над user32 через ctypes. Вне Windows они безопасно возвращают
  «ничего нет» (``None``/``False``), поэтому редактор запускается и на Linux (тесты, разработка).

Клавиши опрашиваются ``GetAsyncKeyState`` (как в MaskOver), а не регистрируются глобально:
Buldozer их тоже получает, зато сочетание не «съедается» у других программ, а реагирует наше
окно только когда активен Buldozer (или само окно).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass

IS_WINDOWS = sys.platform.startswith("win")

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_RBUTTON = 0x02
# модификаторы (общие и левые/правые) — их перехват не глушит, они читаются GetAsyncKeyState
MODIFIER_VKS = frozenset((0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C))

_NAMED_KEYS = {
    "INSERT": 0x2D, "INS": 0x2D, "DELETE": 0x2E, "DEL": 0x2E, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PGUP": 0x21, "PAGEDOWN": 0x22, "PGDN": 0x22, "SPACE": 0x20,
    "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D, "ESC": 0x1B, "ESCAPE": 0x1B,
    "PAUSE": 0x13, "BACKSPACE": 0x08, "`": 0xC0, "~": 0xC0,
    "MBUTTON": 0x04, "MMB": 0x04, "XBUTTON1": 0x05, "XBUTTON2": 0x06,
    "RBUTTON": 0x02, "RMB": 0x02, "LBUTTON": 0x01, "LMB": 0x01, "ЛКМ": 0x01,
    # по-русски (так их и показываем): правая/средняя кнопка, боковые кнопки мыши
    "ПКМ": 0x02, "СКМ": 0x04, "МЫШЬ4": 0x05, "МЫШЬ5": 0x06,
}
# как показывать клавиши (и в каком виде сохранять в настройках)
_PRETTY = {
    0x01: "ЛКМ", 0x02: "ПКМ", 0x04: "СКМ", 0x05: "Мышь4", 0x06: "Мышь5",
    0x2D: "Insert", 0x2E: "Delete", 0x24: "Home", 0x23: "End", 0x21: "PageUp", 0x22: "PageDown",
    0x20: "Space", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x13: "Pause", 0x08: "Backspace",
    0xC0: "`", 0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt",
}
MOUSE_VKS = (0x01, 0x02, 0x04, 0x05, 0x06)
VK_LBUTTON = 0x01
VK_ESCAPE = 0x1B
# сообщения WH_MOUSE_LL кнопок: vk -> (нажата, отпущена); X-кнопки различает старшее слово mouseData
_BUTTON_MSGS = {0x01: (0x0201, 0x0202), 0x02: (0x0204, 0x0205), 0x04: (0x0207, 0x0208),
                0x05: (0x020B, 0x020C), 0x06: (0x020B, 0x020C)}
for _n in range(1, 25):
    _NAMED_KEYS[f"F{_n}"] = 0x6F + _n          # VK_F1 = 0x70
for _n in range(10):
    _NAMED_KEYS[f"NUM{_n}"] = 0x60 + _n


@dataclass(frozen=True)
class Hotkey:
    vk: int
    ctrl: bool = False
    shift: bool = False
    alt: bool = False

    def text(self) -> str:
        mods = [n for n, on in (("Ctrl", self.ctrl), ("Shift", self.shift), ("Alt", self.alt)) if on]
        return "+".join(mods + [vk_name(self.vk)])


def vk_name(vk: int) -> str:
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if vk in _PRETTY:
        return _PRETTY[vk]
    if 0x60 <= vk <= 0x69:
        return f"Num{vk - 0x60}"
    for name, code in _NAMED_KEYS.items():
        if code == vk:
            return name.title()
    return f"VK{vk:#04x}"


def parse_hotkey(text: str) -> Hotkey | None:
    """'F8', 'Shift+F9', 'Ctrl+Alt+P' -> Hotkey; непонятная строка -> None.

    Один модификатор без клавиши ('Ctrl', 'Shift') — сама клавиша-модификатор (для камеры:
    «вниз» = Ctrl, «быстрее» = Shift).
    """
    if not text or not text.strip():
        return None
    ctrl = shift = alt = False
    vk = None
    for part in (p.strip().upper() for p in text.replace("-", "+").split("+")):
        if not part:
            continue
        if part in ("CTRL", "CONTROL"):
            ctrl = True
        elif part == "SHIFT":
            shift = True
        elif part == "ALT":
            alt = True
        elif part in _NAMED_KEYS:
            vk = _NAMED_KEYS[part]
        elif len(part) == 1 and (part.isdigit() or "A" <= part <= "Z"):
            vk = ord(part)
        else:
            return None
    if vk is None and ctrl + shift + alt == 1:
        return Hotkey(VK_CONTROL if ctrl else VK_SHIFT if shift else VK_MENU)
    return Hotkey(vk, ctrl, shift, alt) if vk is not None else None


def hold_match(combos, is_down) -> str | None:
    """Какое из «удерживаемых» сочетаний сейчас зажато: [(имя, Hotkey|None), …] -> имя или None.

    Нужны основная клавиша/кнопка и все модификаторы сочетания; если подходят несколько
    (ПКМ и Alt+ПКМ при зажатом Alt) — выигрывает то, где модификаторов больше.
    """
    best, best_mods = None, -1
    ctrl = shift = alt = None
    for name, hk in combos:
        if hk is None or not is_down(hk.vk):
            continue
        if ctrl is None:
            ctrl, shift, alt = bool(is_down(VK_CONTROL)), bool(is_down(VK_SHIFT)), bool(is_down(VK_MENU))
        if (hk.ctrl and not ctrl) or (hk.shift and not shift) or (hk.alt and not alt):
            continue
        mods = hk.ctrl + hk.shift + hk.alt
        if mods > best_mods:
            best, best_mods = name, mods
    return best


class HotkeyEdge:
    """Срабатывание по нажатию (а не пока клавиша держится).

    Shift должен совпадать точно (F9 и Shift+F9 — разные действия). Ctrl и Alt обязательны,
    только если входят в сочетание; лишние Ctrl/Alt не мешают (AltGr, переключатели раскладки
    и некоторые драйверы клавиатур держат их «нажатыми»).
    """

    def __init__(self, hotkey: Hotkey | None) -> None:
        self.hotkey = hotkey
        self._down = False
        self.pressed = False       # основная клавиша нажата именно в этот опрос
        self.mods = (False, False, False)   # Ctrl, Shift, Alt в момент нажатия

    def update(self, is_down) -> bool:
        """is_down(vk) -> bool. True — в этот опрос сочетание только что нажато."""
        hk = self.hotkey
        if hk is None:
            self.pressed = False
            return False
        main = bool(is_down(hk.vk))
        self.pressed = main and not self._down
        self._down = main
        if not self.pressed:
            return False
        ctrl, shift, alt = bool(is_down(VK_CONTROL)), bool(is_down(VK_SHIFT)), bool(is_down(VK_MENU))
        self.mods = (ctrl, shift, alt)
        return shift == hk.shift and (ctrl or not hk.ctrl) and (alt or not hk.alt)


# ----------------------------------------------------------------- user32 (только Windows)

if IS_WINDOWS:                                             # pragma: no cover - нужен Windows
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]


def _title(hwnd) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def is_buldozer_title(title: str) -> bool:
    """Окно движка в режиме Buldozer называется ровно «Buldozer» (так его ищет и MaskOver).

    Только точное совпадение: «buldozer.c - Блокнот» или папка «Buldozer» в Проводнике — не оно.
    """
    return title.strip().lower() == "buldozer"


def is_engine_exe(path: str) -> bool:
    """DayZ_x64.exe, DayZDiag_x64.exe и т.п. — процесс движка, в котором работает Buldozer."""
    name = path.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    return name.startswith("dayz") and name.endswith(".exe")


def pick_buldozer(windows: list[tuple[object, str, str]]):
    """[(hwnd, заголовок, exe)] в порядке окон -> hwnd Buldozer или None (логика без WinAPI).

    Сначала окно «Buldozer» процесса DayZ*.exe, иначе (exe не прочитался — нет прав) любое
    окно с точным заголовком «Buldozer».
    """
    exact = [(h, exe) for h, title, exe in windows if is_buldozer_title(title)]
    for h, exe in exact:
        if exe and is_engine_exe(exe):
            return h
    for h, exe in exact:
        if not exe:
            return h
    return exact[0][0] if exact else None


def window_title(hwnd) -> str:
    return _title(hwnd) if IS_WINDOWS and hwnd else ""


def _token_elevated(process) -> bool | None:
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):     # TOKEN_QUERY
        return True if ctypes.get_last_error() == 5 else None   # нет доступа — процесс «выше» нас
    try:
        value = wintypes.DWORD()
        size = wintypes.DWORD()
        if not _advapi32.GetTokenInformation(token, 20, ctypes.byref(value), 4, ctypes.byref(size)):
            return None                                          # 20 = TokenElevation
        return bool(value.value)
    finally:
        _kernel32.CloseHandle(token)


def process_elevated(pid: int) -> bool | None:
    """Запущен ли процесс от администратора (None — не узнать). Окна такого процесса не отдают
    обычной программе нажатия клавиш (GetAsyncKeyState — нули) и не пускают её мышь/курсор."""
    if not IS_WINDOWS or not pid:
        return None
    handle = _kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return True if ctypes.get_last_error() == 5 else None
    try:
        return _token_elevated(handle)
    finally:
        _kernel32.CloseHandle(handle)


def self_elevated() -> bool:
    """Эта программа запущена от администратора."""
    return bool(IS_WINDOWS and _token_elevated(_kernel32.GetCurrentProcess()))


# Клавиши Windows (VK) -> имена KeyCode движка (скрипт моста читает их сам через KeyState —
# так F8/F9 работают, даже когда Buldozer запущен от администратора, а программа нет).
VK_TO_KEYCODE: dict[int, tuple[str, ...]] = {
    0x10: ("KC_LSHIFT", "KC_RSHIFT"), 0x11: ("KC_LCONTROL", "KC_RCONTROL"),
    0x12: ("KC_LMENU", "KC_RMENU"),
    0xA0: ("KC_LSHIFT",), 0xA1: ("KC_RSHIFT",), 0xA2: ("KC_LCONTROL",), 0xA3: ("KC_RCONTROL",),
    0xA4: ("KC_LMENU",), 0xA5: ("KC_RMENU",),
    0x08: ("KC_BACK",), 0x09: ("KC_TAB",), 0x0D: ("KC_RETURN",), 0x1B: ("KC_ESCAPE",),
    0x20: ("KC_SPACE",), 0x21: ("KC_PRIOR",), 0x22: ("KC_NEXT",), 0x23: ("KC_END",),
    0x24: ("KC_HOME",), 0x25: ("KC_LEFT",), 0x26: ("KC_UP",), 0x27: ("KC_RIGHT",),
    0x28: ("KC_DOWN",), 0x2D: ("KC_INSERT",), 0x2E: ("KC_DELETE",),
    0x6A: ("KC_MULTIPLY",), 0x6B: ("KC_ADD",), 0x6D: ("KC_SUBTRACT",), 0x6E: ("KC_DECIMAL",),
    0x6F: ("KC_DIVIDE",),
    0xBA: ("KC_SEMICOLON",), 0xBB: ("KC_EQUALS",), 0xBC: ("KC_COMMA",), 0xBD: ("KC_MINUS",),
    0xBE: ("KC_PERIOD",), 0xBF: ("KC_SLASH",), 0xC0: ("KC_GRAVE",), 0xDB: ("KC_LBRACKET",),
    0xDC: ("KC_BACKSLASH",), 0xDD: ("KC_RBRACKET",), 0xDE: ("KC_APOSTROPHE",),
}
VK_TO_KEYCODE.update({0x30 + i: (f"KC_{i}",) for i in range(10)})
VK_TO_KEYCODE.update({0x41 + i: (f"KC_{chr(0x41 + i)}",) for i in range(26)})
VK_TO_KEYCODE.update({0x60 + i: (f"KC_NUMPAD{i}",) for i in range(10)})
VK_TO_KEYCODE.update({0x70 + i: (f"KC_F{i + 1}",) for i in range(12)})


def keycodes_for(vks) -> list[str]:
    """Имена KeyCode для набора VK (мышиные кнопки и незнакомые клавиши пропускаются)."""
    out: list[str] = []
    for vk in vks:
        for name in VK_TO_KEYCODE.get(int(vk), ()):
            if name not in out:
                out.append(name)
    return out


def process_exe(pid: int) -> str:
    """Полный путь exe процесса ('' — не удалось: нет прав или процесс завершился)."""
    if not IS_WINDOWS or not pid:
        return ""
    handle = _kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def find_buldozer_window():
    """HWND видимого окна Buldozer (процесс DayZ) или None."""
    if not IS_WINDOWS:
        return None
    found = []

    def cb(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            title = _title(hwnd)
            if is_buldozer_title(title):
                found.append((hwnd, title, process_exe(window_pid(hwnd))))
        return True

    _user32.EnumWindows(_EnumProc(cb), 0)
    return pick_buldozer(found)


def is_window(hwnd) -> bool:
    return bool(IS_WINDOWS and hwnd and _user32.IsWindow(hwnd))


def is_minimized(hwnd) -> bool:
    return bool(IS_WINDOWS and hwnd and _user32.IsIconic(hwnd))


def client_rect(hwnd) -> tuple[int, int, int, int] | None:
    """Клиентская область окна на экране (физические пиксели): (x, y, ширина, высота)."""
    if not is_window(hwnd):
        return None
    rc = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    pt = wintypes.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top)


def window_pid(hwnd) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def foreground_window():
    return _user32.GetForegroundWindow() if IS_WINDOWS else None


def foreground_is(hwnd) -> bool:
    """Активно ли окно hwnd (или любое окно его процесса — диалоги движка тоже считаются)."""
    if not IS_WINDOWS or not hwnd:
        return False
    fg = _user32.GetForegroundWindow()
    if not fg:
        return False
    if _user32.GetAncestor(fg, 2) == _user32.GetAncestor(hwnd, 2):       # GA_ROOT
        return True
    pid = window_pid(hwnd)
    return pid != 0 and pid == window_pid(fg)


def foreground_is_ours() -> bool:
    """Активно окно нашего процесса (окно поверх Buldozer, редактор, диалог сохранения…)."""
    if not IS_WINDOWS:
        return False
    fg = _user32.GetForegroundWindow()
    return bool(fg) and window_pid(fg) == os.getpid()


def key_down(vk: int) -> bool:
    return bool(IS_WINDOWS and (_user32.GetAsyncKeyState(int(vk)) & 0x8000))


def set_cursor_pos(x: int, y: int) -> bool:
    """Поставить курсор мыши (экранные пиксели). False — Windows не дала (или не Windows)."""
    return bool(IS_WINDOWS and _user32.SetCursorPos(int(x), int(y)))


def screen_size() -> tuple[int, int] | None:
    """Размер основного экрана (физические пиксели) или None."""
    if not IS_WINDOWS:
        return None
    w, h = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    return (w, h) if w > 0 and h > 0 else None


def cursor_pos() -> tuple[int, int] | None:
    """Курсор мыши на экране (физические пиксели) или None."""
    if not IS_WINDOWS:
        return None
    pt = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return (pt.x, pt.y)


def pointer_in(hwnd) -> tuple[float, float] | None:
    """Курсор над окном hwnd (не над нашим меню поверх него): доли клиентской области
    (0..1, слева направо и сверху вниз) или None."""
    if not IS_WINDOWS or not hwnd:
        return None
    pos = cursor_pos()
    rect = client_rect(hwnd)
    if pos is None or rect is None or rect[2] <= 0 or rect[3] <= 0:
        return None
    under = _user32.WindowFromPoint(wintypes.POINT(*pos))
    if not under or _user32.GetAncestor(under, 2) != _user32.GetAncestor(hwnd, 2):
        return None
    u = (pos[0] - rect[0] + 0.5) / rect[2]
    v = (pos[1] - rect[1] + 0.5) / rect[3]
    if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
        return None
    return (u, v)


# ----------------------------------------------------------------- перехват для своей камеры

CLICK_TRAVEL_PX = 6        # щелчок: мышь прошла не больше (пиксели)…
CLICK_MAX_SECONDS = 0.35   # …и кнопку отпустили быстро


class InputHook:
    """Перехват клавиатуры и мыши (WH_KEYBOARD_LL / WH_MOUSE_LL), пока работает своя камера.

    В Buldozer у клавиш свои действия (D — удалить выделенное, C — копировать, V — создать…),
    поэтому клавиши движения, пока ``capture_keys``, до него не доходят (их состояние
    перехватчик помнит сам — ``is_down``). Кнопка обзора (``look_vk``, по умолчанию ЛКМ) над
    окном Buldozer тоже не доходит до него: пока она зажата (``look_held``), ``capture_mouse``
    «съедает» движение мыши — курсор стоит, а сдвиги копятся для поворота камеры (``take``).
    Колесо над Buldozer (``capture_wheel``) — скорость камеры. Над другими окнами (наше меню)
    ничего не перехватывается. Отпускание проходит, если нажатие проходило (ничего не залипнет).

    Работает в своём потоке с очередью сообщений — подвисание окна не тормозит ввод в системе.
    Вне Windows ``install()`` возвращает False.
    """

    def __init__(self, swallow_vks=(), look_vk: int = VK_LBUTTON) -> None:
        self.swallow = frozenset(int(v) for v in swallow_vks
                                 if v and int(v) not in MODIFIER_VKS and int(v) not in MOUSE_VKS)
        self.look_vk = int(look_vk)
        self.capture_keys = False
        self.capture_mouse = False
        self.capture_wheel = False
        self.target = None                # HWND Buldozer: кнопку и колесо берём только над ним
        self._down: set[int] = set()
        self._look_ours = False           # нажатие кнопки обзора проглотили мы
        self._look_at = 0.0               # когда нажали кнопку обзора (для «щелчка»)
        self._look_travel = 0             # сколько пикселей мышь прошла, пока кнопка зажата
        self.clicks = 0                   # короткие щелчки кнопкой обзора без сдвига (выделение)
        self.last_move: tuple[int, int] | None = None   # куда пользователь двинул курсор (не перехвачено)
        self.move_at: float | None = None # когда мышь последний раз двигали (monotonic)
        self.seen: float | None = None    # когда перехватчик последний раз получил событие (monotonic)
        self._lock = threading.Lock()
        self._dx = self._dy = self._wheel = 0
        self._thread = None
        self._tid = 0
        self._hooks = (None, None)
        self._procs = None

    @property
    def installed(self) -> bool:
        return self._thread is not None

    def is_down(self, vk: int) -> bool:
        if vk in self.swallow:
            return vk in self._down
        return key_down(vk)

    @property
    def look_is_button(self) -> bool:
        return self.look_vk in _BUTTON_MSGS

    def look_held(self) -> bool:
        """Зажата кнопка/клавиша обзора (нажатие было над Buldozer, когда он активен)."""
        if self.look_is_button:
            return self._look_ours
        return self.is_down(self.look_vk)

    def take(self) -> tuple[int, int, int]:
        """Накопленные сдвиги мыши (dx, dy в пикселях) и колесо (120 = щелчок), с обнулением."""
        with self._lock:
            out = (self._dx, self._dy, self._wheel)
            self._dx = self._dy = self._wheel = 0
        return out

    def take_clicks(self) -> int:
        """Сколько коротких щелчков кнопкой обзора было (нажали и отпустили, мышь почти стояла)."""
        with self._lock:
            n, self.clicks = self.clicks, 0
        return n

    def _add(self, dx: int, dy: int, wheel: int) -> None:
        with self._lock:
            self._dx += dx
            self._dy += dy
            self._wheel += wheel

    # ---- разбор событий (без WinAPI — тестируется)
    def on_key(self, vk: int, down: bool, injected: bool = False) -> bool:
        """True — событие проглотить (не отдавать Buldozer)."""
        if vk not in self.swallow or injected:
            return False
        if down:
            self._down.add(vk)
            return self.capture_keys
        self._down.discard(vk)
        return False                      # отпускание проходит всегда

    def on_move(self, x: int, y: int, cursor: tuple[int, int] | None) -> bool:
        if not self.capture_mouse or cursor is None:
            return False
        dx, dy = x - cursor[0], y - cursor[1]
        if abs(dx) < 4000 and abs(dy) < 4000:
            self._add(dx, dy, 0)
            if self._look_ours:
                self._look_travel += abs(dx) + abs(dy)
        return True

    def on_wheel(self, delta: int, over_target: bool = True) -> bool:
        if not self.capture_wheel or not over_target:
            return False
        self._add(0, 0, delta)
        return True

    def on_button(self, vk: int, down: bool, over_target: bool = True) -> bool:
        """Кнопка мыши. True — проглотить. Берём только кнопку обзора над окном Buldozer."""
        if vk != self.look_vk:
            return False
        if down:
            if self.capture_keys and over_target:
                self._look_ours = True
                self._look_at = time.monotonic()
                self._look_travel = 0
                self.capture_mouse = True  # сразу: первый же сдвиг — уже поворот камеры
                return True
            return False
        if self._look_ours:                # отпускание того, что проглотили, — тоже наше
            self._look_ours = False
            self.capture_mouse = False     # сразу отдать мышь (не ждать такта камеры)
            if (self._look_travel <= CLICK_TRAVEL_PX
                    and time.monotonic() - self._look_at <= CLICK_MAX_SECONDS):
                with self._lock:
                    self.clicks += 1
            return True
        return False

    # ---- установка
    def install(self) -> bool:
        if self._thread is not None:
            return True
        if not IS_WINDOWS:
            return False
        return self._install_windows()                     # pragma: no cover - нужен Windows

    def uninstall(self) -> None:
        self.capture_keys = self.capture_mouse = self.capture_wheel = False
        self._look_ours = False
        thread, self._thread = self._thread, None
        if thread is None:
            return
        if IS_WINDOWS and self._tid:                       # pragma: no cover - нужен Windows
            _user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)     # WM_QUIT
            thread.join(1.0)
        self._down.clear()
        self.take()

    def _install_windows(self) -> bool:                    # pragma: no cover - нужен Windows
        ready = threading.Event()
        kb_proc = _HOOKPROC(self._kb_cb)
        ms_proc = _HOOKPROC(self._ms_cb)
        self._procs = (kb_proc, ms_proc)                    # держать ссылки, иначе GC

        def run():
            self._tid = _kernel32.GetCurrentThreadId()
            hmod = _kernel32.GetModuleHandleW(None)
            kb = _user32.SetWindowsHookExW(13, kb_proc, hmod, 0)       # WH_KEYBOARD_LL
            ms = _user32.SetWindowsHookExW(14, ms_proc, hmod, 0)       # WH_MOUSE_LL
            self._hooks = (kb, ms)
            ready.set()
            if kb and ms:
                msg = wintypes.MSG()
                while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    pass
            for h in (kb, ms):
                if h:
                    _user32.UnhookWindowsHookEx(h)
            self._hooks = (None, None)

        thread = threading.Thread(target=run, name="we-input-hook", daemon=True)
        self._thread = thread
        thread.start()
        ready.wait(2.0)
        if not all(self._hooks):
            self.uninstall()
            return False
        self.seen = time.monotonic()
        return True

    def _over_target(self, pt) -> bool:                    # pragma: no cover - нужен Windows
        """Под курсором окно Buldozer (а не наше меню поверх него)?"""
        if not self.target:
            return False
        hwnd = _user32.WindowFromPoint(pt)
        return bool(hwnd) and _user32.GetAncestor(hwnd, 2) == _user32.GetAncestor(self.target, 2)

    def _kb_cb(self, n_code, w_param, l_param):            # pragma: no cover - нужен Windows
        self.seen = time.monotonic()
        try:
            if n_code == 0:
                kb = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                down = w_param in (0x0100, 0x0104)          # WM_KEYDOWN, WM_SYSKEYDOWN
                up = w_param in (0x0101, 0x0105)
                if (down or up) and self.on_key(int(kb.vkCode), down, bool(kb.flags & 0x10)):
                    return 1
        except Exception:                                  # noqa: BLE001 - ввод важнее
            pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _ms_cb(self, n_code, w_param, l_param):            # pragma: no cover - нужен Windows
        self.seen = time.monotonic()
        try:
            if n_code == 0:
                ms = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                if w_param == 0x0200:                       # WM_MOUSEMOVE
                    self.move_at = self.seen
                    if self.capture_mouse and self.on_move(ms.pt.x, ms.pt.y, cursor_pos()):
                        return 1
                    self.last_move = (ms.pt.x, ms.pt.y)
                elif w_param == 0x020A:                     # WM_MOUSEWHEEL
                    if self.capture_wheel:
                        delta = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
                        if self.on_wheel(delta, self._over_target(ms.pt)):
                            return 1
                elif self.look_is_button:
                    msgs = _BUTTON_MSGS[self.look_vk]
                    if w_param in msgs:
                        xbtn = (ms.mouseData >> 16) & 0xFFFF
                        if self.look_vk in (0x05, 0x06) and xbtn != self.look_vk - 4:
                            pass                            # другая боковая кнопка
                        else:
                            down = w_param == msgs[0]
                            over = self._over_target(ms.pt) if down else True
                            if self.on_button(self.look_vk, down, over):
                                return 1
        except Exception:                                  # noqa: BLE001
            pass
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)


if IS_WINDOWS:                                             # pragma: no cover - нужен Windows
    _LRESULT = ctypes.c_ssize_t
    _HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    _user32.SetWindowsHookExW.restype = wintypes.HANDLE
    _user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    _user32.CallNextHookEx.restype = _LRESULT
    _user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                    wintypes.UINT]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM]
    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
