"""Application settings stored as JSON in the user's profile (Qt-free)."""
from __future__ import annotations

import json
from pathlib import Path

SETTINGS_FILE = Path.home() / ".dayz_terrain_editor" / "settings.json"

DEFAULTS: dict = {
    "debug": False,
    # ---- пути (стартовое окно, launcher.py) ----
    "dayz_dir": "",             # папка DayZ (с DayZ_x64.exe); пусто = искать в Steam
    "project_dir": "",          # папка проекта карты (P:\Evelone2666): в ней ищется TemplateLibs
    "scripts_dir": "",          # папка скриптов Buldozer (P:\scripts): сюда пишется buldozer.c
    "last_export_dir": "",
    "work_drive": "",           # пусто = диск папки скриптов/проекта, иначе P:\
    "auto_install_script": True,  # обновлять блок моста в buldozer.c при запуске и смене пути
    "start_overlay_watch": True,  # следить за Buldozer и клавишами сразу после запуска
    "launcher_collapsed": [],     # свёрнутые категории стартового окна: paths/overlay/camera/state
    # ---- окно поверх Buldozer (buldozer_overlay.py / buldozer_bridge.py) ----
    "overlay_toggle_key": "F8",     # показать/скрыть окно поверх Buldozer
    "overlay_place_key": "F9",      # поставить выбранную модель в точку под курсором
    "overlay_undo_key": "Shift+F9", # отменить последнее действие окна
    "overlay_profile_dir": "",      # папка $profile: Buldozer; пусто = искать (P:\Buldozer и др.)
    "overlay_autohide": True,       # прятать окно, когда активна другая программа
    "overlay_ghost": True,          # показывать выбранную модель у курсора в Buldozer
    "overlay_icon_size": "medium",  # значки моделей: small / medium / large
    "overlay_library": "",          # выбранная библиотека шаблонов (пусто = все)
    "overlay_template_names": True, # экспорт для TB: имя шаблона (без пути и .p3d)
    "overlay_tb_relative_height": True,  # экспорт для TB: высота над землёй (Relative), 0 = на земле
    "overlay_mouse_adjust": True,   # держать сочетание + двигать мышь: поворот / подъём модели
    "overlay_rotate_key": "ПКМ",    # держать + мышь влево/вправо = поворот
    "overlay_lift_key": "Alt+ПКМ",  # держать + мышь вверх/вниз = подъём/опускание
    "overlay_select_key": "F7",     # выделить объект под курсором (или короткий щелчок ЛКМ при своей камере)
    "overlay_move_key": "Shift+ПКМ",  # держать: выделенный объект едет за курсором
    "overlay_delete_key": "Delete",   # удалить выделенный объект (свой или карты)
    "overlay_layer_offset": None,   # [dx, dz] координаты слоя TB − координаты Buldozer (находится сам)
    "overlay_lock_cursor": True,    # пока держат: курсор (и модель) стоят на месте
    "overlay_mouse_yaw_per_px": 0.5,    # градусов поворота на пиксель движения мыши
    "overlay_mouse_lift_per_px": 0.01,  # метров подъёма на пиксель
    "overlay_script_name": "Buldozer",  # -name= у Buldozer: файл <имя>.c, функция <Имя>Main
    # ---- своя камера в Buldozer (freecam.py): пока открыто окно F8 ----
    "overlay_camera": True,         # WASD + мышь вместо камеры Buldozer
    "overlay_cam_forward_key": "W",
    "overlay_cam_back_key": "S",
    "overlay_cam_left_key": "A",
    "overlay_cam_right_key": "D",
    "overlay_cam_up_key": "Space",
    "overlay_cam_down_key": "Ctrl",
    "overlay_cam_fast_key": "Shift",
    "overlay_cam_look_key": "ЛКМ",  # держать + мышь = обзор (над окном Buldozer)
    "overlay_cam_speed": 30.0,      # м/с (колесо мыши меняет на ходу)
    "overlay_cam_fast_mult": 4.0,   # во сколько раз быстрее с клавишей «быстрее»
    "overlay_cam_sens": 0.12,       # градусов поворота на пиксель мыши
    "overlay_cam_invert_y": False,  # мышь вверх — взгляд вниз
    "overlay_cam_fly_view": False,  # W/S — по направлению взгляда (иначе горизонтально)
}

def load_settings(path: Path | str = SETTINGS_FILE) -> dict:
    """Настройки из файла; неизвестные ключи и значения не того типа — по умолчанию."""
    settings = dict(DEFAULTS)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if not isinstance(data, dict):
        return settings
    for key, default in DEFAULTS.items():
        value = data.get(key)
        if isinstance(default, bool):
            if isinstance(value, bool):
                settings[key] = value
        elif isinstance(default, float):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                settings[key] = float(value)
        elif isinstance(value, type(default)):
            settings[key] = value
    # из старого редактора: путь к DayZ_x64.exe -> папка DayZ
    old_exe = data.get("dayz_exe")
    if not settings["dayz_dir"] and isinstance(old_exe, str) and old_exe.strip():
        old_exe = old_exe.strip()
        if old_exe.lower().endswith(".exe"):
            cut = max(old_exe.rfind("\\"), old_exe.rfind("/"))
            old_exe = old_exe[:cut] if cut > 0 else ""
        settings["dayz_dir"] = old_exe
    return settings


def save_settings(settings: dict, path: Path | str = SETTINGS_FILE) -> None:
    """Write the known keys; other keys in the file are not kept (there are none yet)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {key: settings.get(key, default) for key, default in DEFAULTS.items()}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
