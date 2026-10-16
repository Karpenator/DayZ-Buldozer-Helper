"""Пути: папка DayZ (Steam), рабочий диск P:\\, папка скриптов Buldozer, профиль (без Qt)."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PureWindowsPath
from typing import Iterable

DEFAULT_WORK_DRIVE = "P:\\"
DAYZ_EXE_NAMES = ("DayZ_x64.exe", "DayZDiag_x64.exe")
_LIB_PATH_RE = re.compile(r'"path"\s+"([^"]+)"')


# ---------------------------------------------------------------- DayZ / Steam

def _registry_steam_paths() -> list[Path]:
    if not sys.platform.startswith("win"):
        return []
    try:
        import winreg
    except ImportError:                       # pragma: no cover
        return []
    out = []
    for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
        try:
            with winreg.OpenKey(hive, key) as k:
                raw, _ = winreg.QueryValueEx(k, value)
        except OSError:
            continue
        if raw:
            out.append(Path(str(raw).replace("/", "\\")))
    return out


def steam_roots() -> list[Path]:
    """Где установлен Steam: реестр, затем Program Files."""
    roots: list[Path] = []
    for p in _registry_steam_paths():
        if p not in roots:
            roots.append(p)
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base and (Path(base) / "Steam") not in roots:
            roots.append(Path(base) / "Steam")
    return [r for r in roots if r.is_dir()]


def steam_libraries(vdf_texts: Iterable[str] | None = None, roots: Iterable[Path] | None = None) -> list[Path]:
    """Библиотеки Steam из steamapps\\libraryfolders.vdf (+ сами папки Steam)."""
    libs: list[Path] = []
    roots = list(steam_roots() if roots is None else roots)
    texts = list(vdf_texts) if vdf_texts is not None else []
    for root in roots:
        if root not in libs:
            libs.append(root)
        if vdf_texts is None:
            try:
                texts.append((root / "steamapps" / "libraryfolders.vdf").read_text(
                    encoding="utf-8", errors="replace"))
            except OSError:
                pass
    for text in texts:
        for raw in _LIB_PATH_RE.findall(text):
            lib = Path(raw.replace("\\\\", "\\"))
            if lib not in libs:
                libs.append(lib)
    return libs


def dayz_exe_in(folder: str | Path | None) -> Path | None:
    if not folder:
        return None
    base = Path(folder)
    if base.is_file() and base.name.lower().startswith("dayz"):
        return base
    for name in DAYZ_EXE_NAMES:
        if (base / name).is_file():
            return base / name
    return None


def find_dayz_dir(libraries: Iterable[Path] | None = None) -> Path | None:
    """Папка DayZ (с DayZ_x64.exe) в библиотеках Steam; None — не нашли."""
    for lib in (steam_libraries() if libraries is None else libraries):
        folder = Path(lib) / "steamapps" / "common" / "DayZ"
        if dayz_exe_in(folder):
            return folder
    return None


# ---------------------------------------------------------------- рабочий диск / скрипты

def work_drive_of(*paths: str | Path | None, default: str = DEFAULT_WORK_DRIVE) -> str:
    """Корень рабочего диска: P:\\scripts -> P:\\ (первый путь с буквой диска)."""
    for p in paths:
        if not p:
            continue
        drive = PureWindowsPath(str(p)).drive
        if drive:
            return drive.rstrip("\\/") + "\\"
    return default


def resolve_work_drive(explicit: str | None, *paths: str | Path | None) -> str:
    """Рабочий диск: явная настройка, иначе диск папки скриптов/проекта, иначе P:\\."""
    if explicit and str(explicit).strip():
        e = str(explicit).strip()
        return e if e.endswith(("\\", "/")) else e + ("\\" if PureWindowsPath(e).drive else "/")
    return work_drive_of(*paths)


def default_scripts_dir(work_drive: str = DEFAULT_WORK_DRIVE) -> str:
    return work_drive.rstrip("\\/") + "\\scripts"


def default_profile_dir(work_drive: str = DEFAULT_WORK_DRIVE) -> str:
    return work_drive.rstrip("\\/") + "\\Buldozer"


def model_file(work_drive: str, model: str) -> Path:
    """Путь модели из шаблона TB (от корня рабочего диска) -> файл на диске."""
    rel = model.replace("/", "\\").strip().lstrip("\\")
    if PureWindowsPath(rel).drive:           # в шаблоне уже полный путь
        return Path(rel)
    return Path(work_drive.rstrip("\\/") + "\\" + rel) if sys.platform.startswith("win") \
        else Path(work_drive.rstrip("\\/")) / Path(*PureWindowsPath(rel).parts)


def texture_resolver(work_drive: str):
    """Функция «имя текстуры из модели -> файл .paa на рабочем диске или None»."""
    cache: dict[str, Path | None] = {}

    def resolve(name: str) -> Path | None:
        key = name.lower()
        if key not in cache:
            p = model_file(work_drive, name)
            cache[key] = p if p.is_file() else None
        return cache[key]

    return resolve
