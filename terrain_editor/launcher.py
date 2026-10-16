"""Стартовое окно DayZ World Editor: пути, настройки и кнопки для работы с Buldozer.

Всё, что нужно один раз указать, и всё, что делается автоматически:

* **папка DayZ** — ищется в Steam сама (проверяем DayZ_x64.exe);
* **папка проекта карты** — в ней сами находим ``TemplateLibs\\*.tml`` и берём оттуда модели
  для окна над Buldozer (имена шаблонов — для экспорта в TB);
* **папка скриптов** (``P:\\scripts``) — сюда сам пишется/обновляется ``buldozer.c`` с мостом.

Окно над Buldozer (``buldozer_overlay.py``) работает, пока открыто это окно: F8 в Buldozer.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from . import debuglog, dialogs
from . import winbuldozer as wb
from .buldozer_bridge import (SCRIPT_REVISION, freshest_state, install_script,
                              profile_candidates, script_path_in, script_status)
from .buldozer_overlay import (CAMERA_KEYS, DEFAULT_DELETE_KEY, DEFAULT_LIFT_KEY, DEFAULT_LOOK_KEY,
                               DEFAULT_MOVE_KEY, DEFAULT_ROTATE_KEY, DEFAULT_SELECT_KEY,
                               BuldozerOverlay)
from .hotkeyedit import HotkeyEdit
from .dayzpaths import (dayz_exe_in, default_profile_dir, default_scripts_dir, find_dayz_dir,
                        resolve_work_drive)
from .settings import load_settings, save_settings
from .templates import TemplateCatalog

log = debuglog.get_logger("launcher")

APP_TITLE = "DayZ World Editor"
KEYS_HINT = ("Чтобы сменить сочетание: щёлкните по полю и нажмите клавиши (можно с Ctrl/Shift/Alt). "
             "Кнопка мыши — щёлкните по полю нужной кнопкой (ПКМ — правая, СКМ — средняя, "
             "Мышь4/5 — боковые), тоже можно с Alt/Ctrl/Shift. Backspace — по умолчанию, Esc — отмена.")
LAUNCHER_QSS = """
QMainWindow, QWidget { background: #23262b; color: #dde3ea; font-size: 12px; }
QLineEdit { background: #1a1c20; border: 1px solid #444a53; border-radius: 3px; padding: 4px; }
QLineEdit[bad="true"] { border: 1px solid #d0603a; }
QPushButton { background: #353a42; border: 1px solid #4a515b; border-radius: 3px; padding: 5px 10px; }
QPushButton:hover { background: #414955; }
QPushButton#primary { background: #2f5e8f; font-weight: bold; }
QLabel#title { font-size: 16px; font-weight: bold; color: #ffffff; }
QLabel#ok { color: #8fd18f; }
QLabel#warn { color: #ffb454; }
QLabel#bad { color: #ff7b6b; }
QLabel#hint { color: #a9b3bf; }
QPushButton#section { background: transparent; border: none; color: #9fc3ea; font-weight: bold;
                      font-size: 13px; text-align: left; padding: 6px 2px 4px 2px; }
QPushButton#section:hover { color: #cfe3fb; }
QFrame#sectionBody { border: 1px solid #3a3f47; border-radius: 5px; }
"""


def _set_state(label: QLabel, text: str, kind: str) -> None:
    label.setText(text)
    label.setObjectName(kind)
    label.style().unpolish(label)
    label.style().polish(label)


class PathRow:
    """Строка «подпись — поле — Обзор… [— доп. кнопка]» + строка состояния под ней."""

    def __init__(self, grid: QGridLayout, row: int, title: str, placeholder: str, tip: str,
                 browse_title: str, extra: QPushButton | None = None) -> None:
        self.label = QLabel(title)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setToolTip(tip)
        self.browse = QPushButton("Обзор…")
        self.browse_title = browse_title
        self.state = QLabel()
        self.state.setObjectName("hint")
        self.state.setWordWrap(True)
        grid.addWidget(self.label, row, 0)
        grid.addWidget(self.edit, row, 1)
        grid.addWidget(self.browse, row, 2)
        if extra is not None:
            grid.addWidget(extra, row, 3)
        grid.addWidget(self.state, row + 1, 1, 1, 3)

    def text(self) -> str:
        return self.edit.text().strip()


class Section(QWidget):
    """Сворачиваемая категория: заголовок-кнопка (▼/▶) и рамка с содержимым (``.box``)."""

    toggled = Signal(bool)

    def __init__(self, title: str, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(2)
        self.header = QPushButton()
        self.header.setObjectName("section")
        self.header.setCheckable(True)
        self.header.setChecked(True)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setToolTip("Щёлкните, чтобы свернуть или развернуть")
        self.box = QFrame()
        self.box.setObjectName("sectionBody")
        lay.addWidget(self.header)
        lay.addWidget(self.box)
        self.header.toggled.connect(self._toggled)
        self._show(True)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, on: bool) -> None:
        self.header.setChecked(bool(on))       # сигнал toggled — только если состояние сменилось

    def _show(self, on: bool) -> None:
        self.header.setText(("▼  " if on else "▶  ") + self.title)
        self.box.setVisible(on)

    def _toggled(self, on: bool) -> None:
        self._show(on)
        self.toggled.emit(on)


class LauncherWindow(QMainWindow):
    def __init__(self, catalog: TemplateCatalog | None = None,
                 overlay: BuldozerOverlay | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} — расстановка объектов в Buldozer")
        self.setStyleSheet(LAUNCHER_QSS)
        screen = QApplication.primaryScreen()
        height = min(980, int(screen.availableGeometry().height() * 0.92)) if screen else 900
        self.resize(800, height)
        self.catalog = catalog if catalog is not None else TemplateCatalog()
        self.overlay = overlay if overlay is not None else BuldozerOverlay(self.catalog, parent=self)
        self._loading = True

        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(14, 12, 14, 12)
        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        root.addWidget(title)
        intro = QLabel("1. Укажите папки ниже (один раз). 2. Откройте карту в Terrain Builder и "
                       "запустите из него Buldozer. 3. В Buldozer нажмите F10 (один раз за сеанс), "
                       "затем F8 — окно с моделями, F9 — поставить модель под курсор.")
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        root.addWidget(intro)

        # ---- пути
        paths = Section("Папки", "paths")
        grid = QGridLayout(paths.box)
        grid.setColumnStretch(1, 1)
        self.find_dayz = QPushButton("Найти")
        self.find_dayz.setToolTip("Найти DayZ в библиотеках Steam")
        self.dayz = PathRow(grid, 0, "Папка DayZ:", r"E:\Steam\steamapps\common\DayZ",
                            "Где установлена игра (там лежит DayZ_x64.exe)", "Папка DayZ",
                            self.find_dayz)
        self.reload_templates = QPushButton("Обновить")
        self.reload_templates.setToolTip("Перечитать TemplateLibs\\*.tml (если меняли библиотеки в TB)")
        self.project = PathRow(grid, 2, "Папка проекта карты:", r"P:\Evelone2666",
                               "Папка проекта Terrain Builder: в ней ищется TemplateLibs с *.tml",
                               "Папка проекта карты", self.reload_templates)
        self.install_now = QPushButton("Записать")
        self.install_now.setToolTip("Записать/обновить мост в buldozer.c прямо сейчас")
        self.scripts = PathRow(grid, 4, "Папка скриптов:", r"P:\scripts",
                               "Сюда пишется buldozer.c — скрипт, который Buldozer запускает по F10",
                               "Папка скриптов Buldozer (P:\\scripts)", self.install_now)
        self.open_profile = QPushButton("Открыть")
        self.open_profile.setToolTip("Открыть папку профиля (логи Buldozer, файлы моста)")
        self.profile = PathRow(grid, 6, "Профиль Buldozer:", "авто (P:\\Buldozer)",
                               "Папка из параметра -profiles= у Buldozer. Обычно указывать не нужно.",
                               "Папка профиля Buldozer ($profile:)", self.open_profile)
        root.addWidget(paths)

        # ---- окно над Buldozer
        opts = Section("Окно над Buldozer", "overlay")
        og = QGridLayout(opts.box)
        og.setColumnStretch(1, 1)
        og.setColumnStretch(3, 1)
        # (поле, ключ настроек, по умолчанию, подпись)
        self.hotkey_specs = (
            ("key_toggle", "overlay_toggle_key", "F8", "Показать/скрыть окно:"),
            ("key_place", "overlay_place_key", "F9", "Поставить модель:"),
            ("key_undo", "overlay_undo_key", "Shift+F9", "Отменить:"),
            ("key_select", "overlay_select_key", DEFAULT_SELECT_KEY, "Выделить объект под курсором:"),
            ("key_rotate", "overlay_rotate_key", DEFAULT_ROTATE_KEY, "Поворот (держать + мышь ↔):"),
            ("key_lift", "overlay_lift_key", DEFAULT_LIFT_KEY, "Подъём (держать + мышь ↕):"),
            ("key_move", "overlay_move_key", DEFAULT_MOVE_KEY, "Перемещать выделенный (держать):"),
            ("key_delete", "overlay_delete_key", DEFAULT_DELETE_KEY, "Удалить выделенный:"),
        )
        cells = ((0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1))
        for (attr, _key, default, label), (row, col) in zip(self.hotkey_specs, cells):
            edit = HotkeyEdit(default)
            edit.setMinimumWidth(110)
            setattr(self, attr, edit)
            og.addWidget(QLabel(label), row, col * 2)
            og.addWidget(edit, row, col * 2 + 1)
        self.yaw_speed = QDoubleSpinBox()
        self.yaw_speed.setRange(0.05, 5.0)
        self.yaw_speed.setSingleStep(0.05)
        self.yaw_speed.setSuffix(" ° на пиксель")
        self.yaw_speed.setToolTip("На сколько градусов поворачивается модель при движении мыши на 1 пиксель")
        self.lift_speed = QDoubleSpinBox()
        self.lift_speed.setRange(0.1, 20.0)
        self.lift_speed.setSingleStep(0.1)
        self.lift_speed.setDecimals(1)
        self.lift_speed.setSuffix(" см на пиксель")
        self.lift_speed.setToolTip("На сколько сантиметров поднимается модель при движении мыши на 1 пиксель")
        og.addWidget(QLabel("Скорость поворота:"), 4, 0)
        og.addWidget(self.yaw_speed, 4, 1)
        og.addWidget(QLabel("Скорость подъёма:"), 4, 2)
        og.addWidget(self.lift_speed, 4, 3)
        self.keys_hint = QLabel(KEYS_HINT)
        self.keys_hint.setObjectName("hint")
        self.keys_hint.setWordWrap(True)
        og.addWidget(self.keys_hint, 5, 0, 1, 4)
        self.chk_autohide = QCheckBox("Прятать окно, когда активна другая программа")
        self.chk_mouse = QCheckBox("Поворот и подъём модели мышью (сочетания выше)")
        self.chk_lock = QCheckBox("Пока держите сочетание поворота/подъёма — курсор и модель стоят на месте")
        self.chk_lock.setToolTip("Модель не ездит за мышью, пока её крутят или поднимают; после "
                                 "отпускания курсор остаётся там же, и F9 ставит модель туда.")
        self.chk_names = QCheckBox("Экспорт для TB: имена шаблонов (а не пути к .p3d)")
        self.chk_relative = QCheckBox("Экспорт для TB: высота над землёй (Relative, 0 = на земле)")
        self.chk_autoinstall = QCheckBox("Обновлять buldozer.c автоматически (при запуске и смене папки)")
        self.chk_debug = QCheckBox("Журнал отладки (для поиска ошибок)")
        for i, chk in enumerate((self.chk_autohide, self.chk_mouse, self.chk_lock, self.chk_names,
                                 self.chk_relative, self.chk_autoinstall, self.chk_debug)):
            og.addWidget(chk, 6 + i, 0, 1, 4)
        root.addWidget(opts)

        # ---- своя камера
        cam = Section("Своя камера в Buldozer (пока открыто окно F8)", "camera")
        cg = QGridLayout(cam.box)
        cg.setColumnStretch(1, 1)
        cg.setColumnStretch(3, 1)
        self.chk_camera = QCheckBox("Включить: зажатая кнопка обзора + мышь — поворот камеры, "
                                    "клавиши ниже — движение")
        self.chk_camera.setToolTip("Пока работает камера, клавиши движения, кнопка обзора и колесо "
                                   "над Buldozer до него не доходят (в Buldozer D удаляет "
                                   "выделенное, C копирует, ЛКМ выделяет и т.д.).")
        cg.addWidget(self.chk_camera, 0, 0, 1, 4)
        cam_labels = {"forward": "Вперёд:", "back": "Назад:", "left": "Влево:", "right": "Вправо:",
                      "up": "Вверх:", "down": "Вниз:", "fast": "Быстрее (держать):"}
        cam_specs = (("key_cam_look", "overlay_cam_look_key", DEFAULT_LOOK_KEY,
                      "Обзор (держать + мышь):"),)
        cam_specs += tuple((f"key_cam_{name}", key, default, cam_labels[name])
                           for name, key, default in CAMERA_KEYS)
        for i, (attr, _key, default, label) in enumerate(cam_specs):
            edit = HotkeyEdit(default, allow_modifier=True, allow_left=attr == "key_cam_look")
            edit.setMinimumWidth(110)
            setattr(self, attr, edit)
            row, col = 1 + i // 2, i % 2
            cg.addWidget(QLabel(label), row, col * 2)
            cg.addWidget(edit, row, col * 2 + 1)
        self.hotkey_specs = self.hotkey_specs + cam_specs
        self.cam_speed = QDoubleSpinBox()
        self.cam_speed.setRange(0.5, 2000.0)
        self.cam_speed.setDecimals(1)
        self.cam_speed.setSuffix(" м/с")
        self.cam_speed.setToolTip("Скорость полёта. Колесо мыши в Buldozer меняет её на ходу.")
        self.cam_sens = QDoubleSpinBox()
        self.cam_sens.setRange(0.01, 2.0)
        self.cam_sens.setDecimals(2)
        self.cam_sens.setSingleStep(0.01)
        self.cam_sens.setSuffix(" ° на пиксель")
        self.cam_sens.setToolTip("Чувствительность мыши: на сколько градусов поворачивается камера")
        cg.addWidget(QLabel("Скорость:"), 6, 0)
        cg.addWidget(self.cam_speed, 6, 1)
        cg.addWidget(QLabel("Чувствительность:"), 6, 2)
        cg.addWidget(self.cam_sens, 6, 3)
        self.chk_cam_view = QCheckBox("Вперёд/назад — по направлению взгляда (иначе горизонтально)")
        self.chk_cam_invert = QCheckBox("Инвертировать мышь по вертикали")
        cg.addWidget(self.chk_cam_view, 7, 0, 1, 4)
        cg.addWidget(self.chk_cam_invert, 8, 0, 1, 4)
        root.addWidget(cam)

        # ---- слои Terrain Builder
        lay = Section("Слои Terrain Builder", "layers")
        lg = QGridLayout(lay.box)
        lg.setColumnStretch(1, 1)
        self.layer_combo = QComboBox()
        self.layer_combo.setToolTip("Слой проекта (*.tv4l), в который записываются поставленные "
                                    "объекты — как активный слой в Terrain Builder")
        lg.addWidget(QLabel("Слой для новых объектов:"), 0, 0)
        lg.addWidget(self.layer_combo, 0, 1, 1, 3)
        self.chk_layers_auto = QCheckBox("Сразу записывать в рабочую копию слоёв (после каждой "
                                         "установки и правки)")
        lg.addWidget(self.chk_layers_auto, 1, 0, 1, 4)
        self.st_layers = QLabel()
        self.st_layers.setWordWrap(True)
        self.st_layers.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lg.addWidget(self.st_layers, 2, 0, 1, 4)
        self.btn_layers_write = QPushButton("Записать сейчас")
        self.btn_layers_apply = QPushButton("Применить в проект…")
        self.btn_layers_apply.setToolTip("Заменить слои проекта рабочей копией (Terrain Builder "
                                         "должен быть закрыт; прежние слои — в резервную копию)")
        self.btn_layers_open = QPushButton("Открыть папку")
        row = QHBoxLayout()
        for b in (self.btn_layers_write, self.btn_layers_apply, self.btn_layers_open):
            row.addWidget(b)
        row.addStretch(1)
        lg.addLayout(row, 3, 0, 1, 4)
        hint = QLabel("Пока открыт Terrain Builder, он не видит изменённых файлов и при сохранении "
                      "проекта перезапишет их, поэтому всё пишется в рабочую копию "
                      "(WE_edited_layers\\current рядом с проектом). Закройте TB и нажмите "
                      "«Применить в проект» — затем откройте проект снова.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lg.addWidget(hint, 4, 0, 1, 4)
        root.addWidget(lay)

        # ---- состояние
        state = Section("Состояние", "state")
        sl = QVBoxLayout(state.box)
        self.st_buldozer = QLabel()
        self.st_link = QLabel()
        self.st_session = QLabel()
        self.st_thumbs = QLabel()
        for lab in (self.st_buldozer, self.st_link, self.st_session, self.st_thumbs):
            lab.setWordWrap(True)
            sl.addWidget(lab)
        root.addWidget(state)
        self.sections = (paths, opts, cam, lay, state)

        # ---- кнопки
        buttons = QHBoxLayout()
        self.btn_overlay = QPushButton("Показать окно над Buldozer")
        self.btn_overlay.setObjectName("primary")
        self.btn_export = QPushButton("Экспорт для Terrain Builder…")
        self.btn_clear_thumbs = QPushButton("Очистить кэш превью")
        for b in (self.btn_overlay, self.btn_export, self.btn_clear_thumbs):
            buttons.addWidget(b)
        root.addLayout(buttons)
        root.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        self.setCentralWidget(scroll)

        self._load_into_widgets()
        self._connect()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._update_live_state)
        self._loading = False

    # ------------------------------------------------------------ настройки <-> поля
    def _load_into_widgets(self) -> None:
        s = load_settings()
        self.dayz.edit.setText(s.get("dayz_dir", ""))
        self.project.edit.setText(s.get("project_dir", ""))
        self.scripts.edit.setText(s.get("scripts_dir", ""))
        self.profile.edit.setText(s.get("overlay_profile_dir", ""))
        for attr, key, default, _label in self.hotkey_specs:
            hk = wb.parse_hotkey(s.get(key) or default)
            getattr(self, attr).setText(hk.text() if hk else default)
        self.yaw_speed.setValue(float(s.get("overlay_mouse_yaw_per_px") or 0.5))
        self.lift_speed.setValue(float(s.get("overlay_mouse_lift_per_px") or 0.01) * 100.0)
        self.chk_lock.setChecked(bool(s.get("overlay_lock_cursor", True)))
        self.chk_layers_auto.setChecked(bool(s.get("overlay_layers_auto", True)))
        self.chk_autohide.setChecked(bool(s.get("overlay_autohide", True)))
        self.chk_mouse.setChecked(bool(s.get("overlay_mouse_adjust", True)))
        self.chk_names.setChecked(bool(s.get("overlay_template_names", True)))
        self.chk_relative.setChecked(bool(s.get("overlay_tb_relative_height", True)))
        self.chk_autoinstall.setChecked(bool(s.get("auto_install_script", True)))
        self.chk_debug.setChecked(bool(s.get("debug", False)))
        self.chk_camera.setChecked(bool(s.get("overlay_camera", True)))
        self.chk_cam_view.setChecked(bool(s.get("overlay_cam_fly_view", False)))
        self.chk_cam_invert.setChecked(bool(s.get("overlay_cam_invert_y", False)))
        self.cam_speed.setValue(float(s.get("overlay_cam_speed") or 30.0))
        self.cam_sens.setValue(float(s.get("overlay_cam_sens") or 0.12))
        collapsed = {k for k in s.get("launcher_collapsed") or () if isinstance(k, str)}
        for sec in self.sections:
            sec.set_expanded(sec.key not in collapsed)

    def _connect(self) -> None:
        for row, key in ((self.dayz, "dayz_dir"), (self.project, "project_dir"),
                         (self.scripts, "scripts_dir"), (self.profile, "overlay_profile_dir")):
            row.edit.editingFinished.connect(lambda r=row, k=key: self._path_edited(r, k))
            row.browse.clicked.connect(lambda _=False, r=row, k=key: self._browse(r, k))
        self.find_dayz.clicked.connect(self.auto_find_dayz)
        self.reload_templates.clicked.connect(self.load_templates)
        self.install_now.clicked.connect(lambda: self.ensure_script(force=True, ask=True))
        self.open_profile.clicked.connect(self._open_profile_dir)
        for attr, key, _default, _label in self.hotkey_specs:
            edit = getattr(self, attr)
            edit.hotkeyChanged.connect(lambda _text, e=edit, k=key: self._key_edited(e, k))
        self.yaw_speed.valueChanged.connect(
            lambda v: self._number("overlay_mouse_yaw_per_px", round(v, 3)))
        self.lift_speed.valueChanged.connect(
            lambda v: self._number("overlay_mouse_lift_per_px", round(v / 100.0, 4)))
        self.cam_speed.valueChanged.connect(lambda v: self._number("overlay_cam_speed", round(v, 2)))
        self.cam_sens.valueChanged.connect(lambda v: self._number("overlay_cam_sens", round(v, 3)))
        for chk, key in ((self.chk_autohide, "overlay_autohide"), (self.chk_mouse, "overlay_mouse_adjust"),
                         (self.chk_lock, "overlay_lock_cursor"),
                         (self.chk_names, "overlay_template_names"),
                         (self.chk_relative, "overlay_tb_relative_height"),
                         (self.chk_autoinstall, "auto_install_script"), (self.chk_debug, "debug"),
                         (self.chk_camera, "overlay_camera"), (self.chk_cam_view, "overlay_cam_fly_view"),
                         (self.chk_cam_invert, "overlay_cam_invert_y")):
            chk.toggled.connect(lambda on, k=key: self._flag(k, on))
        for sec in self.sections:
            sec.toggled.connect(lambda _on: self._save_sections())
        self.btn_overlay.clicked.connect(self.overlay.toggle)
        self.btn_export.clicked.connect(self.overlay.export_for_tb)
        self.btn_clear_thumbs.clicked.connect(self.clear_thumbs)
        self.layer_combo.currentIndexChanged.connect(
            lambda *_: self.overlay.set_target_layer(self.layer_combo.currentText()))
        self.chk_layers_auto.toggled.connect(self.overlay.window.layers_auto.setChecked)
        self.overlay.window.layers_auto.toggled.connect(self._sync_layers_auto)
        self.overlay.layersChanged.connect(self._update_layers_state)
        self.btn_layers_write.clicked.connect(lambda: self.overlay.write_layers(self))
        self.btn_layers_apply.clicked.connect(lambda: self.overlay.apply_layers_to_project(self))
        self.btn_layers_open.clicked.connect(self._open_layers_dir)

    def _save(self, key: str, value) -> None:
        s = load_settings()
        if s.get(key) == value:
            return
        s[key] = value
        save_settings(s)
        log.info("setting %s = %r", key, value)

    def _save_sections(self) -> None:
        if not self._loading:
            self._save("launcher_collapsed", [sec.key for sec in self.sections if not sec.is_expanded()])

    def _flag(self, key: str, on: bool) -> None:
        if self._loading:
            return
        self._save(key, bool(on))
        if key == "debug":
            if on:
                debuglog.enable()
            else:
                debuglog.disable()
        self.overlay.reload_settings()
        if key == "auto_install_script" and on:
            self.ensure_script()

    def _number(self, key: str, value: float) -> None:
        if self._loading:
            return
        self._save(key, float(value))
        self.overlay.reload_settings()

    def _key_edited(self, edit: QLineEdit, key: str) -> None:
        """Проверить и сохранить сочетание. Занятое другим действием — не сохраняем."""
        hk = wb.parse_hotkey(edit.text())
        problem = "" if hk is not None else f"«{edit.text()}» — не похоже на сочетание клавиш"
        if hk is not None:
            for attr, other_key, _default, label in self.hotkey_specs:
                other = getattr(self, attr)
                if other is not edit and wb.parse_hotkey(other.text()) == hk:
                    problem = f"{hk.text()} уже занято: {label.rstrip(':')}"
                    break
        edit.setProperty("bad", bool(problem))
        edit.style().unpolish(edit)
        edit.style().polish(edit)
        if problem:
            _set_state(self.keys_hint, problem + ". Выберите другое сочетание.", "bad")
            return
        _set_state(self.keys_hint, f"Сохранено: {hk.text()}. " + KEYS_HINT, "hint")
        edit.setText(hk.text())
        self._save(key, hk.text())
        self.overlay.reload_settings()

    def _browse(self, row: PathRow, key: str) -> None:
        start = row.text() or (self.work_drive() if Path(self.work_drive()).exists() else str(Path.home()))
        folder = dialogs.pick_folder(self, row.browse_title, start)
        if folder:
            row.edit.setText(os.path.normpath(folder))
            self._path_edited(row, key)

    def _path_edited(self, row: PathRow, key: str) -> None:
        value = os.path.normpath(row.text()) if row.text() else ""
        if value != row.text():
            row.edit.setText(value)
        changed = load_settings().get(key) != value
        self._save(key, value)
        self.overlay.set_work_drive(self.work_drive())
        if key == "project_dir" and changed:
            self.load_templates()
            self.overlay.refresh_layer_list()
        elif key == "scripts_dir":
            if changed and self.chk_autoinstall.isChecked():
                self.ensure_script()
            self._update_script_state()
        elif key == "dayz_dir":
            self._update_dayz_state()
        elif key == "overlay_profile_dir":
            self.overlay.reload_settings()
            self._update_profile_state()
        if key in ("scripts_dir", "project_dir"):
            self._update_profile_state()

    # ------------------------------------------------------------ автоматизация
    def work_drive(self) -> str:
        s = load_settings()
        return resolve_work_drive(s.get("work_drive"), self.scripts.text(), self.project.text())

    def auto_fill(self) -> None:
        """Заполнить пустые пути тем, что нашлось само (DayZ в Steam, P:\\scripts)."""
        if not self.dayz.text():
            self.auto_find_dayz(quiet=True)
        if not self.scripts.text():
            drive = self.work_drive()
            if Path(drive).is_dir():
                self.scripts.edit.setText(default_scripts_dir(drive))
                self._save("scripts_dir", self.scripts.text())

    def auto_find_dayz(self, quiet: bool = False) -> None:
        found = find_dayz_dir()
        if found is not None:
            self.dayz.edit.setText(str(found))
            self._save("dayz_dir", str(found))
        elif not quiet:
            dialogs.info(self, "Папка DayZ", "DayZ в библиотеках Steam не найден. "
                                    "Укажите папку игры вручную (там лежит DayZ_x64.exe).")
        self._update_dayz_state()

    def load_templates(self) -> None:
        project = self.project.text()
        if project and not Path(project).is_dir():
            self.catalog.load(None)
            self.catalog.project_dir = Path(project)
        else:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.catalog.load(project or None)
            finally:
                QApplication.restoreOverrideCursor()
        self.overlay.set_catalog(self.catalog)
        self._update_project_state()

    def ensure_script(self, force: bool = False, ask: bool = False) -> None:
        """Поставить/обновить блок моста в <папка скриптов>\\buldozer.c."""
        folder = self.scripts.text()
        if not folder:
            if ask:
                dialogs.info(self, "Скрипт", "Сначала укажите папку скриптов (обычно P:\\scripts).")
            self._update_script_state()
            return
        path = script_path_in(folder, load_settings().get("overlay_script_name") or "Buldozer")
        state, _rev = script_status(path)
        if state == "ok" and not force:
            self._update_script_state()
            return
        if state == "newer" and not force:
            self._update_script_state()
            return
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
            result = install_script(path, load_settings().get("overlay_script_name") or "Buldozer")
        except OSError as exc:
            log.warning("script install failed: %s", exc)
            self._update_script_state(error=str(exc))
            if ask:
                dialogs.error(self, "Скрипт", f"Не удалось записать {path}:\n{exc}")
            return
        log.info("bridge script %s: %s", result.action, result.path)
        self._update_script_state(note=result.describe().split("\n")[0])
        if ask:
            dialogs.info(self, "Скрипт для Buldozer", result.describe() +
                                    "\n\nЕсли Buldozer уже открыт — перезапустите его из Terrain Builder "
                                    "(скрипт читается при запуске) и нажмите F10.")

    def clear_thumbs(self) -> None:
        n = self.overlay.clear_thumbnails()
        self._update_thumbs_state(f"удалено файлов: {n}")

    def _open_profile_dir(self) -> None:
        folder = self.profile.text() or default_profile_dir(self.work_drive())
        if Path(folder).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            dialogs.info(self, "Профиль Buldozer", f"Папка не найдена: {folder}")

    def _sync_layers_auto(self, on: bool) -> None:
        if self.chk_layers_auto.isChecked() != on:
            self.chk_layers_auto.blockSignals(True)
            self.chk_layers_auto.setChecked(on)
            self.chk_layers_auto.blockSignals(False)

    def _open_layers_dir(self) -> None:
        work = self.overlay.layers_work_dir()
        folder = work if work is not None and work.is_dir() else (work.parent.parent if work else None)
        if folder is not None and folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            dialogs.info(self, "Слои", "Папка слоёв проекта (<Проект>.Layers) не найдена — укажите "
                                       "папку проекта карты.")

    def _update_layers_state(self) -> None:
        ov = self.overlay
        src = ov.window.layer
        names = [src.itemText(i) for i in range(src.count())]
        combo = self.layer_combo
        combo.blockSignals(True)
        if [combo.itemText(i) for i in range(combo.count())] != names:
            combo.clear()
            combo.addItems(names)
        combo.setCurrentIndex(src.currentIndex())
        combo.setEnabled(bool(names))
        combo.blockSignals(False)
        self._sync_layers_auto(ov.window.layers_auto.isChecked())
        work = ov.layers_work_dir()
        if work is None:
            _set_state(self.st_layers, "Папка слоёв проекта (<Проект>.Layers) не найдена.", "warn")
            ok = False
        else:
            files = sorted(work.glob("*.tv4l")) if work.is_dir() else []
            if files:
                when = time.strftime("%H:%M:%S", time.localtime(max(p.stat().st_mtime for p in files)))
                _set_state(self.st_layers, f"Рабочая копия ({when}): {', '.join(p.name for p in files)} — "
                                           f"{work}", "ok")
            else:
                _set_state(self.st_layers, f"Рабочая копия пуста (изменений нет) — {work}", "hint")
            ok = bool(files) or bool(ov.session.objects) or bool(ov.session.changed_map_edits())
        self.btn_layers_write.setEnabled(work is not None)
        self.btn_layers_apply.setEnabled(ok)

    # ------------------------------------------------------------ строки состояния
    def refresh_states(self) -> None:
        self._update_dayz_state()
        self._update_project_state()
        self._update_script_state()
        self._update_profile_state()
        self._update_thumbs_state()
        self._update_layers_state()
        self._update_live_state()

    def _update_dayz_state(self) -> None:
        folder = self.dayz.text()
        exe = dayz_exe_in(folder)
        if exe is not None:
            _set_state(self.dayz.state, f"Найдено: {exe.name}", "ok")
        elif folder:
            _set_state(self.dayz.state, "В этой папке нет DayZ_x64.exe", "bad")
        else:
            _set_state(self.dayz.state, "Не указана — нажмите «Найти» или «Обзор…»", "warn")

    def _update_project_state(self) -> None:
        project = self.project.text()
        if not project:
            _set_state(self.project.state, "Не указана: моделей в окне не будет. Выберите папку "
                       "проекта карты (например P:\\Evelone2666).", "warn")
            return
        if not Path(project).is_dir():
            _set_state(self.project.state, "Папка не найдена (подключён ли рабочий диск P:?)", "bad")
            return
        libs = self.catalog.libraries
        if not libs:
            _set_state(self.project.state, "TemplateLibs\\*.tml в этой папке не найдено", "bad")
            return
        names = ", ".join(lib.name for lib in libs[:4]) + (" …" if len(libs) > 4 else "")
        broken = [lib for lib in libs if lib.error]
        text = (f"Библиотек: {len(libs)} ({names}), моделей: {len(self.catalog.templates)}")
        if broken:
            text += f". С ошибками: {len(broken)} (прочитаны частично)"
        _set_state(self.project.state, text, "warn" if broken else "ok")

    def _update_script_state(self, note: str = "", error: str = "") -> None:
        folder = self.scripts.text()
        drive = self.work_drive()
        if not folder:
            _set_state(self.scripts.state, "Не указана: Buldozer не сможет связаться с окном "
                       f"(обычно {default_scripts_dir(drive)}).", "warn")
            return
        if error:
            _set_state(self.scripts.state, f"Не удалось записать buldozer.c: {error}", "bad")
            return
        path = script_path_in(folder, load_settings().get("overlay_script_name") or "Buldozer")
        state, rev = script_status(path)
        text = {
            "ok": f"{path.name}: мост v{rev} на месте",
            "missing": f"{path.name} ещё нет — нажмите «Записать»",
            "no_block": f"{path.name} есть, но моста в нём нет — нажмите «Записать» "
                        "(ваш код сохранится)",
            "old": f"{path.name}: старый мост v{rev}, нужен v{SCRIPT_REVISION} — нажмите «Записать»",
            "newer": f"{path.name}: мост v{rev} новее этой программы — обновите программу",
        }[state]
        if note:
            text = f"{note}. {text}"
        if state == "ok":
            text += ". После обновления перезапустите Buldozer и нажмите F10."
        _set_state(self.scripts.state, text, "ok" if state == "ok" else "warn")

    def _update_profile_state(self) -> None:
        s = load_settings()
        folder, age = freshest_state(profile_candidates(s))
        if folder is not None and age is not None and age < 5:
            _set_state(self.profile.state, f"Скрипт Buldozer пишет сюда: {folder}", "ok")
        elif self.profile.text() and not Path(self.profile.text()).is_dir():
            _set_state(self.profile.state, "Папка не найдена", "bad")
        else:
            where = self.profile.text() or default_profile_dir(self.work_drive())
            _set_state(self.profile.state, f"Жду связи со скриптом в {where} "
                       "(Buldozer из TB + F10)", "hint")

    def _update_thumbs_state(self, note: str = "") -> None:
        count, size = self.overlay.thumbs.cache.stats()
        text = f"Превью в кэше: {count} ({size / 1048576:.1f} МБ) — {self.overlay.thumbs.cache.folder}"
        if note:
            text += f"; {note}"
        _set_state(self.st_thumbs, text, "hint")

    def _update_live_state(self) -> None:
        ov = self.overlay
        if not wb.IS_WINDOWS:
            _set_state(self.st_buldozer, "Buldozer: слежение работает только в Windows", "hint")
        elif ov._bz:
            _set_state(self.st_buldozer, f"Buldozer: найден ({ov._bz_info})", "ok")
        else:
            _set_state(self.st_buldozer, "Buldozer: не запущен (запустите его из Terrain Builder)", "warn")
        if ov._connected:
            _set_state(self.st_link, f"Связь со скриптом: есть ({ov._state_dir})", "ok")
        else:
            _set_state(self.st_link, "Связь со скриптом: нет — в Buldozer нажмите F10", "warn")
        _set_state(self.st_session, f"Поставлено в этом сеансе: {len(ov.session.objects)}", "hint")
        self.btn_overlay.setText("Скрыть окно над Buldozer" if ov._wanted else "Показать окно над Buldozer")
        if self._status_timer.isActive() and self._tick_profile():
            self._update_profile_state()
            self._update_layers_state()

    def _tick_profile(self) -> bool:
        self._profile_ticks = getattr(self, "_profile_ticks", 0) + 1
        return self._profile_ticks % 6 == 0

    # ------------------------------------------------------------ запуск / выход
    def start(self) -> None:
        """После показа окна: автозаполнение, TemplateLibs, скрипт, слежение за Buldozer."""
        self.auto_fill()
        self.overlay.set_work_drive(self.work_drive())
        self.load_templates()
        if self.chk_autoinstall.isChecked():
            self.ensure_script()
        if load_settings().get("start_overlay_watch", True):
            self.overlay.start()
        self.refresh_states()
        self._status_timer.start()

    def closeEvent(self, event) -> None:
        self._status_timer.stop()
        self.overlay.stop()
        self.overlay.thumbs.stop()
        self.overlay.window.close()
        super().closeEvent(event)
        QApplication.instance().quit()


def run(argv: list[str]) -> int:
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName(APP_TITLE)
    debuglog.install_excepthook()
    if load_settings().get("debug"):
        debuglog.enable()
    win = LauncherWindow()
    win.show()
    QTimer.singleShot(0, win.start)
    return app.exec()


if __name__ == "__main__":                      # pragma: no cover
    sys.exit(run(sys.argv))
