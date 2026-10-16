"""Окно поверх Buldozer: модели проекта (шаблоны TB) и расстановка объектов прямо в Buldozer.

Сценарий: карта открыта в Terrain Builder, Buldozer запущен из TB.

1. Стартовое окно (``launcher.py``) само ставит блок моста в ``P:\\scripts\\buldozer.c``
   и читает список моделей из ``TemplateLibs\\*.tml`` проекта.
2. В Buldozer нажать **F10** (раз за сеанс) — скрипт начинает сообщать точку под курсором.
3. **F8** (в Buldozer) — показать/скрыть это окно. Выбрать модель — она появляется у курсора
   («призрак»); **F9** — поставить, **Shift+F9** — отменить; правая кнопка мыши — поворот,
   Alt+правая — подъём.
4. «Экспорт для Terrain Builder…» — текстовый файл для импорта объектов в проект TB.
6. Объекты, которые уже стоят на карте (и свои): **F7** или короткий щелчок ЛКМ — выделить
   объект под курсором; ПКМ / Alt+ПКМ — поворот / подъём выделенного, Shift+ПКМ (держать) —
   перемещать за курсором. Правки объектов карты пишутся в **копии** слоёв ``*.tv4l``
   (``map_edits.py``, ``tv4l.py``); свои объекты — сразу в экспорт.
5. Пока окно открыто — своя камера (``freecam.py``): зажатая ЛКМ + мышь — обзор (курсор стоит),
   WASD — движение, Пробел/Ctrl — вверх/вниз, Shift — быстрее, колесо — скорость. Эти клавиши,
   ЛКМ и колесо над Buldozer до него не доходят.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListView, QListWidget, QListWidgetItem,
                               QPushButton, QScrollArea, QSplitter, QStyledItemDelegate,
                               QStyleOptionViewItem, QToolButton, QVBoxLayout, QWidget)

from . import debuglog
from . import winbuldozer as wb
from .buldozer_bridge import (CAM_FILE, CMD_FILE, STATE_FILE, STATE_MAX_AGE, PlacementSession,
                              format_commands, freshest_state, new_generation, parse_state,
                              placement_values, profile_candidates)
from . import layer_sync
from . import tblive
from .map_edits import MapEdit, norm_model
from .tv4l import find_layers_dir
from .tbproject import load_project
from .dayzpaths import model_file, resolve_work_drive
from .freecam import CamInput, FreeCamera
from .gizmo import AXES, AxisDrag, axis_at
from .settings import load_settings, save_settings
from .templates import TemplateCatalog, model_key
from . import dialogs
from .thumbcache import RENDER_SIZE
from .thumbloader import ThumbnailLoader

log = debuglog.get_logger("overlay")

MAX_LIST = 4000          # столько моделей показываем в списке (остальное — уточнить фильтром)
# Размеры значков списка моделей: id -> (подпись, размер картинки, плитками?)
DEFAULT_ROTATE_KEY = "ПКМ"         # держать + мышь влево/вправо
DEFAULT_LIFT_KEY = "Alt+ПКМ"      # держать + мышь вверх/вниз
DEFAULT_SELECT_KEY = "F7"          # выделить объект под курсором (с камерой — ещё щелчок ЛКМ)
DEFAULT_MOVE_KEY = "Shift+ПКМ"     # держать: выделенный объект едет за курсором
DEFAULT_DELETE_KEY = "Delete"      # удалить выделенный объект
GIZMO_WRITE_EVERY = 0.05           # с: пока тянут стрелку, файл команд — не чаще
CONNECT_GRACE = 6.0                # с: перебой связи со скриптом, который камера переживает
GUARD_JUMP_PX = 40                 # курсор прыгнул дальше (без движения мыши) — его двинул не человек
GUARD_QUIET = 0.05                 # с: столько мышь не двигали — значит, прыжок не от руки
GUARD_CENTER_PX = 4                # прыжок «в центр» окна Buldozer/экрана — возвращаем назад
GUARD_MAX_FIXES = 10               # столько возвратов за GUARD_WINDOW с — игра держит курсор, не спорим
GUARD_WINDOW = 2.0
GUARD_LEARN = 2                    # прыжок в одну и ту же точку столько раз — это точка «возврата» игры
GUARD_TARGET_PX = 3
GUARD_MAX_TARGETS = 8
DRAG_JUMP_PX = 100                 # поворот/подъём: за один опрос дальше — и в точку возврата — не рука
GUARD_PAUSE = 5.0
HOOK_SILENCE = 0.7                 # с: курсор движется, а перехватчик молчит — Windows его сняла
DEFAULT_TARGET_LAYER = "Decorations"   # слой для новых объектов по умолчанию (как в проектах TB)
LAYERS_WRITE_DELAY = 1.0           # с: пауза в изменениях, после которой пишется рабочая копия слоёв
PICK_TIMEOUT = 2.0                 # с: скрипт не ответил на выбор (старый мост без v10?)
# своя камера: (имя, ключ настроек, по умолчанию) — порядок как у полей CamInput
CAMERA_KEYS = (
    ("forward", "overlay_cam_forward_key", "W"),
    ("back", "overlay_cam_back_key", "S"),
    ("left", "overlay_cam_left_key", "A"),
    ("right", "overlay_cam_right_key", "D"),
    ("up", "overlay_cam_up_key", "Space"),
    ("down", "overlay_cam_down_key", "Ctrl"),
    ("fast", "overlay_cam_fast_key", "Shift"),
)
DEFAULT_LOOK_KEY = "ЛКМ"           # держать + мышь = обзор камеры
CAM_CONFIRM_TIMEOUT = 3.0          # с: скрипт не сообщил «FC 1» — камеру отдаём Buldozer

# (подпись кнопки, размер значка, подписи под значками) — все виды сеткой
ICON_SIZES = {
    "small": ("Мелкие", 40, False),     # только значки, имя — во всплывающей подсказке
    "medium": ("Средние", 96, True),
    "large": ("Крупные", 160, True),
}
SMALL_CELL_PAD = 6                     # мелкие: отступ вокруг значка в клетке сетки
ALL_LIBRARIES = "Все библиотеки"


@dataclass
class Selection:
    """Выделенный в Buldozer объект: свой (id расстановки) или объект карты (правка)."""
    session: int                  # сеанс скрипта
    ref: int                      # номер объекта в реестре скрипта
    model: str
    own: int = -1                 # id своего объекта; −1 — объект карты
    edit: MapEdit | None = None

    @property
    def is_map(self) -> bool:
        return self.edit is not None


def _short(model: str) -> str:
    return model.replace("/", "\\").rsplit("\\", 1)[-1]


ELEVATED_TEXT = ("Buldozer запущен от администратора, а программа — нет: Windows не даёт ей клавиши "
                 "и мышь Buldozer. F8/F9/Shift+F9 передаёт скрипт моста (v15 — обновите и нажмите "
                 "F10); для своей камеры, поворота мышью и Esc запустите программу от администратора.")
TB_MARKS = {"sent": "  … в TB", "ok": "  ✓ в TB", "fail": "  ✗ не в TB"}


def _layers_error_text(rep) -> str:
    """Текст ошибки записи слоёв; если слои не разобрались — что делать дальше."""
    text = f"Не записано: {rep.error}"
    if any("не разобран" in e for e in rep.load_errors):
        text += ("\n\nЭтот вариант файла слоя программа пока не понимает (сами слои не тронуты). "
                 "Подробности записаны в лог (debug_*.log) — пришлите его. Пока объекты можно "
                 "перенести через «Экспорт для Terrain Builder…» и импорт в TB.")
    return text


def _same_edit(a: MapEdit, b: MapEdit) -> bool:
    return (a.model, a.x0, a.z0, a.yaw0) == (b.model, b.x0, b.z0, b.yaw0)


OVERLAY_QSS = """
QWidget { background: #262626; color: #dddddd; font-size: 12px; }
QToolButton { background: #3a3a3a; border: 1px solid #4d4d4d; border-radius: 3px; padding: 2px 6px; }
QToolButton:checked { background: #2f5e8f; }
QComboBox { background: #1c1c1c; border: 1px solid #444444; border-radius: 3px; padding: 2px 4px; }
QLineEdit, QListWidget, QDoubleSpinBox { background: #1c1c1c; border: 1px solid #444444;
    border-radius: 3px; padding: 3px; selection-background-color: #3b6ea5; }
QPushButton { background: #3a3a3a; border: 1px solid #4d4d4d; border-radius: 3px; padding: 4px 8px; }
QPushButton:hover { background: #46505c; }
QPushButton#place { background: #2f5e8f; font-weight: bold; }
QLabel#title { font-weight: bold; font-size: 13px; color: #ffffff; }
QLabel#status { color: #b8c4d0; }
QLabel#warn { color: #ffb454; }
"""


def native_to_logical(x: int, y: int) -> QPoint:
    """Экранные физические пиксели (WinAPI) -> логические координаты Qt.

    Qt6 на Windows сохраняет начало каждого экрана в физических пикселях и масштабирует
    координаты внутри экрана на его devicePixelRatio.
    """
    for screen in QGuiApplication.screens():
        g = screen.geometry()
        dpr = screen.devicePixelRatio() or 1.0
        native = QRect(g.x(), g.y(), int(g.width() * dpr), int(g.height() * dpr))
        if native.contains(x, y):
            return QPoint(g.x() + int((x - g.x()) / dpr), g.y() + int((y - g.y()) / dpr))
    return QPoint(x, y)


def native_rect_to_logical(rect: tuple[int, int, int, int]) -> QRect:
    x, y, w, h = rect
    tl = native_to_logical(x, y)
    br = native_to_logical(x + w - 1, y + h - 1)
    return QRect(tl, br)


def _positive(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _placeholder_icon() -> QIcon:
    """Серый квадрат на месте превью (пока рисуется или если модель не читается)."""
    # Размер как у превью (RENDER_SIZE): Qt не увеличивает маленькие иконки, и при одинаковых
    # размерах строк плитки считались бы по заглушке — подписи под превью пропадали.
    size = RENDER_SIZE
    pm = QPixmap(size, size)
    pm.fill(QColor(58, 62, 68))
    p = QPainter(pm)
    p.setPen(QColor(110, 116, 124))
    p.drawRect(0, 0, size - 1, size - 1)
    font = p.font()
    font.setPixelSize(size // 4)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignCenter, "p3d")
    p.end()
    return QIcon(pm)


class IconOnlyDelegate(QStyledItemDelegate):
    """Рисует в списке моделей только значок (без имени) — для мелких значков сеткой.
    Имя у элемента остаётся (поиск, подсказка), просто не выводится."""

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        option.text = ""
        option.features &= ~QStyleOptionViewItem.HasDisplay


class CompactList(QListWidget):
    """Список, которому много места не нужно: растягивается, если есть, но не требует 192 px."""

    def sizeHint(self) -> QSize:
        return QSize(200, 72)


class OverlayWindow(QWidget):
    """Само окно. Логика (Buldozer, клавиши, файлы моста) — в BuldozerOverlay."""

    closed = Signal()
    placeClicked = Signal()
    undoClicked = Signal()
    deleteClicked = Signal()
    clearClicked = Signal()
    exportClicked = Signal()
    selResetClicked = Signal()
    selClearClicked = Signal()
    saveLayersClicked = Signal()
    editsClearClicked = Signal()
    targetLayerChanged = Signal(str)
    layersAutoToggled = Signal(bool)
    multiToggled = Signal(bool)
    iconSizeChanged = Signal(str)
    paramsChanged = Signal()
    modelChanged = Signal()
    userMoved = Signal()

    def __init__(self) -> None:
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("DayZ World Editor — поверх Buldozer")
        self.setStyleSheet(OVERLAY_QSS)
        self.setWindowOpacity(0.95)
        self.resize(420, 760)
        self._programmatic_move = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        title = QLabel("Расстановка объектов")
        title.setObjectName("title")
        root.addWidget(title)
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(self.status.fontMetrics().lineSpacing() * 4 + 4)
        root.addWidget(self.status)
        self.warn = QLabel()
        self.warn.setObjectName("warn")
        self.warn.setWordWrap(True)
        self.warn.hide()
        root.addWidget(self.warn)

        split = QSplitter(Qt.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Поиск модели…")
        self.filter.setClearButtonEnabled(True)
        self.multi = QCheckBox("Множество объектов")
        self.multi.setToolTip("Включено — после установки (F9) модель остаётся у курсора, можно "
                              "ставить ещё. Выключено — модель после установки снимается с курсора.")
        row = QHBoxLayout()
        row.addWidget(self.filter, 1)
        row.addWidget(self.multi)
        tl.addLayout(row)
        row = QHBoxLayout()
        self.library = QComboBox()
        self.library.setToolTip("Библиотека шаблонов Terrain Builder (TemplateLibs\\*.tml)")
        self.library.addItem(ALL_LIBRARIES, "")
        row.addWidget(self.library, 1)
        self.size_buttons = QButtonGroup(self)
        self.size_buttons.setExclusive(True)
        for key, (label, _px, _names) in ICON_SIZES.items():
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setToolTip(f"{label} значки")
            b.setProperty("size_key", key)
            self.size_buttons.addButton(b)
            row.addWidget(b)
        self.size_buttons.buttonClicked.connect(
            lambda b: self.iconSizeChanged.emit(str(b.property("size_key"))))
        tl.addLayout(row)
        self.models = QListWidget()
        self.models.setSelectionMode(QAbstractItemView.SingleSelection)
        self.models.setUniformItemSizes(True)
        self.models.setLayoutMode(QListView.Batched)
        self._text_delegate = QStyledItemDelegate(self.models)    # средние/крупные: значок + имя
        self._icon_delegate = IconOnlyDelegate(self.models)       # мелкие: только значок
        self.models.setBatchSize(200)
        self.models.setMinimumHeight(110)          # окно Buldozer невысокое — уменьшается список
        tl.addWidget(self.models, 1)
        self.models_count = QLabel()
        self.models_count.setObjectName("status")
        tl.addWidget(self.models_count)
        split.addWidget(top)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        self.yaw = QDoubleSpinBox()
        self.yaw.setRange(0.0, 359.9)
        self.yaw.setWrapping(True)
        self.yaw.setDecimals(1)
        self.yaw.setSingleStep(15.0)
        self.yaw.setSuffix(" °")
        self.random_yaw = QCheckBox("случайный")
        row = QHBoxLayout()
        row.addWidget(self.yaw, 1)
        row.addWidget(self.random_yaw)
        form.addRow("Поворот:", row)
        self.scale = QDoubleSpinBox()
        self.scale.setRange(0.05, 20.0)
        self.scale.setDecimals(2)
        self.scale.setSingleStep(0.05)
        self.scale.setValue(1.0)
        self.scale_jitter = QDoubleSpinBox()
        self.scale_jitter.setRange(0.0, 90.0)
        self.scale_jitter.setDecimals(0)
        self.scale_jitter.setSuffix(" %")
        self.scale_jitter.setToolTip("Случайный разброс масштаба ±%")
        row = QHBoxLayout()
        row.addWidget(self.scale, 1)
        row.addWidget(QLabel("±"))
        row.addWidget(self.scale_jitter)
        form.addRow("Масштаб:", row)
        self.lift = QDoubleSpinBox()
        self.lift.setRange(-100.0, 100.0)
        self.lift.setDecimals(2)
        self.lift.setSingleStep(0.1)
        self.lift.setSuffix(" м")
        self.lift.setToolTip("0 — низ модели на земле; минус — утопить, плюс — поднять")
        form.addRow("Подъём:", self.lift)
        self.ghost = QCheckBox("Показывать модель у курсора в Buldozer")
        form.addRow(self.ghost)
        bl.addLayout(form)

        # выделенный объект (свой или уже стоявший на карте)
        self.sel_label = QLabel()
        self.sel_label.setObjectName("status")
        self.sel_label.setWordWrap(True)
        bl.addWidget(self.sel_label)
        row = QHBoxLayout()
        self.sel_reset = QPushButton("Вернуть на место")
        self.sel_reset.setToolTip("Объект карты — туда, где он стоял (свои объекты — «Отменить»)")
        self.sel_clear = QPushButton("Снять выделение")
        row.addWidget(self.sel_reset)
        row.addWidget(self.sel_clear)
        bl.addLayout(row)
        # слои Terrain Builder: куда записываются новые объекты (как активный слой в TB)
        row = QHBoxLayout()
        row.addWidget(QLabel("Слой TB:"))
        self.layer = QComboBox()
        self.layer.setToolTip("Слой проекта Terrain Builder (*.tv4l), в который записываются "
                              "поставленные объекты. Передвинутые объекты карты остаются в своих слоях.")
        row.addWidget(self.layer, 1)
        self.layers_auto = QCheckBox("сразу записывать")
        self.layers_auto.setToolTip("После каждой установки/правки обновлять рабочую копию слоёв "
                                    "(папка WE_edited_layers\\current рядом с проектом TB)")
        row.addWidget(self.layers_auto)
        bl.addLayout(row)
        row = QHBoxLayout()
        self.tb_live = QCheckBox("сразу в открытый TB")
        self.tb_live.setToolTip("Каждый поставленный объект сразу вставляется в открытый Terrain "
                                "Builder — в его активный слой (как перетаскивание файла объектов в "
                                "окно TB). TB сам показывает его в Buldozer; наша копия убирается, "
                                "когда объект TB появился на её месте.")
        self.tb_save = QCheckBox("сохранять проект TB")
        self.tb_save.setToolTip("Через несколько секунд после вставки — команда «Сохранить» "
                                "Terrain Builder: объекты сразу попадают в файлы слоёв проекта.")
        row.addWidget(self.tb_live)
        row.addWidget(self.tb_save)
        row.addStretch(1)
        bl.addLayout(row)
        row = QHBoxLayout()
        self.save_layers = QPushButton("Записать в слои")
        self.save_layers.setToolTip("Записать свои объекты и передвинутые объекты карты в рабочую "
                                    "копию слоёв (*.tv4l). Оригиналы проекта не меняются — в проект "
                                    "их переносит «Применить в проект» в стартовом окне (при "
                                    "закрытом Terrain Builder).")
        self.edits_clear = QPushButton("Забыть правки")
        self.edits_clear.setToolTip("Вернуть все объекты карты на место в Buldozer и очистить "
                                    "список правок")
        row.addWidget(self.save_layers, 1)
        row.addWidget(self.edits_clear)
        bl.addLayout(row)
        self.layers_status = QLabel()
        self.layers_status.setObjectName("status")
        self.layers_status.setWordWrap(True)
        bl.addWidget(self.layers_status)

        self.place = QPushButton("Поставить под курсор")
        self.place.setObjectName("place")
        bl.addWidget(self.place)
        row = QHBoxLayout()
        self.undo = QPushButton("Отменить")
        self.delete = QPushButton("Удалить выбранные")
        row.addWidget(self.undo)
        row.addWidget(self.delete)
        bl.addLayout(row)
        self.placed_label = QLabel()
        self.placed_label.setObjectName("status")
        bl.addWidget(self.placed_label)
        self.placed = CompactList()
        self.placed.setSelectionMode(QAbstractItemView.ExtendedSelection)
        bl.addWidget(self.placed, 1)
        row = QHBoxLayout()
        self.export = QPushButton("Экспорт для Terrain Builder…")
        self.clear = QPushButton("Очистить")
        row.addWidget(self.export, 1)
        row.addWidget(self.clear)
        bl.addLayout(row)
        self.hint = QLabel()
        self.hint.setObjectName("status")
        self.hint.setWordWrap(True)
        bl.addWidget(self.hint)
        # мало места (невысокое окно Buldozer) — низ прокручивается, а не сплющивает поля
        self.placed.setMinimumHeight(48)
        lower = QScrollArea()
        lower.setWidget(bottom)
        lower.setWidgetResizable(True)
        lower.setFrameShape(QFrame.NoFrame)
        lower.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        split.addWidget(lower)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 1)              # высоту окна меняет список моделей
        split.setStretchFactor(1, 0)
        split.setSizes([380, bottom.minimumSizeHint().height()])
        self._split, self._split_fitted = split, False
        root.addWidget(split, 1)

        self.place.clicked.connect(self.placeClicked)
        self.undo.clicked.connect(self.undoClicked)
        self.delete.clicked.connect(self.deleteClicked)
        self.clear.clicked.connect(self.clearClicked)
        self.export.clicked.connect(self.exportClicked)
        self.sel_reset.clicked.connect(self.selResetClicked)
        self.sel_clear.clicked.connect(self.selClearClicked)
        self.save_layers.clicked.connect(self.saveLayersClicked)
        self.edits_clear.clicked.connect(self.editsClearClicked)
        self.layer.currentIndexChanged.connect(
            lambda *_: self.targetLayerChanged.emit(self.layer.currentText()))
        self.layers_auto.toggled.connect(self.layersAutoToggled)
        self.multi.toggled.connect(self.multiToggled)
        for w in (self.yaw, self.scale, self.scale_jitter, self.lift):
            w.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        for w in (self.random_yaw, self.ghost):
            w.toggled.connect(lambda *_: self.paramsChanged.emit())
        self.models.currentItemChanged.connect(lambda *_: self.modelChanged.emit())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._split_fitted:
            QTimer.singleShot(0, self.fit_split)

    def fit_split(self) -> None:
        """Один раз: низу — сколько ему нужно (если влезает), остальное — списку моделей."""
        split = self._split
        total = sum(split.sizes())
        if total <= 0:
            return
        self._split_fitted = True
        bottom = split.widget(1).widget()
        need = bottom.heightForWidth(split.width()) if bottom.hasHeightForWidth() else -1
        need = max(need, bottom.minimumSizeHint().height())     # подписи переносятся по ширине
        top = max(split.widget(0).minimumSizeHint().height(), total - need, int(total * 0.4))
        split.setSizes([top, max(0, total - top)])

    def set_icon_size(self, key: str) -> None:
        """Вид списка моделей — всегда сетка: мелкие — только значки, средние/крупные — с именами."""
        label, px, names = ICON_SIZES.get(key, ICON_SIZES["medium"])
        for b in self.size_buttons.buttons():
            b.setChecked(b.property("size_key") == key)
        lw = self.models
        lw.setViewMode(QListWidget.IconMode)
        lw.setFlow(QListView.LeftToRight)
        lw.setWrapping(True)
        lw.setResizeMode(QListWidget.Adjust)
        lw.setMovement(QListWidget.Static)
        lw.setIconSize(QSize(px, px))
        if names:
            lw.setItemDelegate(self._text_delegate)
            lw.setWordWrap(True)
            lw.setGridSize(QSize(px + 16, px + 36))
            lw.setSpacing(2)
        else:
            lw.setItemDelegate(self._icon_delegate)
            lw.setWordWrap(False)
            lw.setGridSize(QSize(px + SMALL_CELL_PAD, px + SMALL_CELL_PAD))
            lw.setSpacing(1)
        lw.doItemsLayout()

    def move_programmatically(self, pos: QPoint) -> None:
        if self.pos() == pos:
            return
        self._programmatic_move = True
        try:
            self.move(pos)
        finally:
            self._programmatic_move = False

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if not self._programmatic_move and self.isVisible():
            self.userMoved.emit()

    def closeEvent(self, event) -> None:
        super().closeEvent(event)
        self.closed.emit()


class BuldozerOverlay(QObject):
    """Следит за окном Buldozer, клавишами и файлами моста; ведёт сеанс расстановки."""

    layersChanged = Signal()          # список слоёв / выбранный слой / рабочая копия изменились

    def __init__(self, catalog: TemplateCatalog | None = None,
                 session: PlacementSession | None = None, parent: QObject | None = None,
                 loader: ThumbnailLoader | None = None) -> None:
        super().__init__(parent)
        s0 = load_settings()
        self.work_drive = resolve_work_drive(s0.get("work_drive"), s0.get("scripts_dir"),
                                             s0.get("project_dir"))
        self.catalog = catalog if catalog is not None else TemplateCatalog()
        self.thumbs = loader if loader is not None else ThumbnailLoader(work_drive=self.work_drive,
                                                                        parent=self)
        self.thumbs.ready.connect(self._on_thumb_ready)
        self._items_by_model: dict[str, list[QListWidgetItem]] = {}
        self._placeholder = _placeholder_icon()
        self.session = session if session is not None else PlacementSession()
        self.session.load()
        self.window = OverlayWindow()
        self._wanted = False              # пользователь хочет видеть окно (F8 / меню)
        self._bz = None                   # HWND окна Buldozer
        self._bz_info = ""                # «DayZ_x64.exe 22692» — чьё окно нашли
        self._bz_elevated = False         # Buldozer от администратора, а мы нет
        self._drag = None                 # точка курсора на экране, пока держат сочетание поворота/подъёма
        self._drag_mode = None            # "rotate" | "lift"
        self._warp = False                # возвращаем курсор в _drag (курсор и модель стоят на месте)
        self._pin = None                  # (x, y, z) — где «призрак» закреплён на время поворота/подъёма
        self._bz_rect: QRect | None = None
        self._offset: QPoint | None = None   # позиция окна относительно угла Buldozer
        self._state = None
        self._state_dir: Path | None = None
        self._connected = False
        self._connected_at = -1e9         # когда состояние скрипта было свежим (monotonic)
        self._generation = new_generation()
        self._dirty = True
        self._written_dir: Path | None = None
        self._next = (0.0, 1.0)          # поворот/масштаб следующего объекта (уже «брошены»)
        self._tick_no = 0
        self._message = ""
        self._filled_for: tuple | None = None
        # своя камера (пока открыто окно): поза, перехват клавиш/мыши, состояние курсора
        self.camera = FreeCamera()
        self._hook = None
        self._hook_factory = wb.InputHook
        self._cam_disabled = ""           # почему камера выключена до следующего F8
        self._cam_mouse = False           # мышь сейчас у камеры (зажата ЛКМ — обзор, курсор стоит)
        self._cam_script_flags = (False, False, False, False)   # CU/MF/UM/UH от скрипта (журнал)
        self._cam_jumps = 0               # сколько раз курсор «вернули» без нас (журнал)
        self._guard_pos = None            # где курсор должен быть (сторож возврата в центр)
        self._guard_move = None           # последнее движение мыши, которое сторож уже учёл
        self._guard_fixes: list[float] = []   # когда сторож возвращал курсор (для паузы)
        self._guard_paused_until = 0.0
        self._guard_fixed = 0             # сколько раз вернул всего (журнал/строка состояния)
        self._jump_seen: dict[tuple[int, int], int] = {}   # куда прыгал курсор (от центра окна) — сколько раз
        self._cam_retakes = 0             # FR от скрипта: мышь возвращали игре (журнал)
        self._hook_cursor = None          # где был курсор на прошлом такте (сторож перехвата)
        self._hook_restarts = 0
        self._cam_keys = False            # клавиши движения сейчас у камеры
        self._cam_last: float | None = None
        self._cam_started = 0.0
        self._cam_off_text = ""           # «off» ещё не записан (скрипт держал файл)
        # объекты карты (скрипт v10): выделение, перенос, правки
        self._sel: Selection | None = None
        self._gizmo_drag: AxisDrag | None = None    # тянут стрелку гизмо (ЛКМ)
        self._gizmo_hot: int | None = None          # стрелка под курсором / которую тянут
        self._gizmo_write_at = 0.0
        self._look_prev = False                     # ЛКМ над Buldozer была зажата в прошлый такт
        self._pick_seq = new_generation()   # номер запроса «выбрать под курсором» (K|n)
        self._pick_pending: tuple[int, float] | None = None
        # рабочая копия слоёв TB (свои объекты + правки карты), пишется в фоне после изменений
        self._layers_written_sig = None   # что уже записано
        self._layers_seen_sig = None      # что видели на прошлом такте
        self._layers_due = 0.0            # когда записать (после паузы в изменениях)
        self._layers_job = None           # (Future, подпись)
        self._layers_pool = None
        self._layers_report: layer_sync.SyncReport | None = None
        self._layers_dir_cache: tuple[str, Path | None] = ("", None)

        s = load_settings()
        w = self.window
        w.ghost.setChecked(bool(s.get("overlay_ghost", True)))
        w.multi.setChecked(bool(s.get("overlay_multi_place", False)))
        w.layers_auto.setChecked(bool(s.get("overlay_layers_auto", True)))
        w.tb_live.setChecked(bool(s.get("overlay_tb_live", True)))
        w.tb_save.setChecked(bool(s.get("overlay_tb_save", True)))
        self.tblive = tblive.LiveImporter(tblive.default_api(), self._tb_folder,
                                          enabled=w.tb_live.isChecked(), save=w.tb_save.isChecked())
        # объекты прошлого сеанса в TB сами не отправляем: их могли уже перенести экспортом
        self.tblive.skip = {o.id for o in self.session.objects if not o.tb}
        self._icon_size = s.get("overlay_icon_size") if s.get("overlay_icon_size") in ICON_SIZES else "medium"
        w.set_icon_size(self._icon_size)
        self._library_filter = s.get("overlay_library") or ""
        self._load_keys(s)

        w.closed.connect(self._on_closed)
        w.placeClicked.connect(self.place_at_cursor)
        w.undoClicked.connect(self.undo)
        w.deleteClicked.connect(self.delete_selected)
        w.clearClicked.connect(self.clear_session)
        w.exportClicked.connect(self.export_for_tb)
        w.selResetClicked.connect(self.revert_selection)
        w.selClearClicked.connect(lambda: self.set_selection(None))
        w.saveLayersClicked.connect(self.save_to_layers)
        w.editsClearClicked.connect(self.forget_map_edits)
        w.targetLayerChanged.connect(self._on_target_layer)
        w.layersAutoToggled.connect(lambda on: self._save_flag("overlay_layers_auto", on))
        w.tb_live.toggled.connect(lambda on: self._save_flag("overlay_tb_live", on))
        w.tb_save.toggled.connect(lambda on: self._save_flag("overlay_tb_save", on))
        w.multiToggled.connect(lambda on: self._save_flag("overlay_multi_place", on))
        w.iconSizeChanged.connect(self.set_icon_size)
        w.library.currentIndexChanged.connect(self._on_library_changed)
        w.models.verticalScrollBar().valueChanged.connect(lambda *_: self._icons_timer.start())
        w.paramsChanged.connect(self._on_params)
        w.modelChanged.connect(self._on_model_changed)
        w.userMoved.connect(self._remember_offset)
        w.filter.textChanged.connect(lambda *_: self._filter_timer.start())

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(200)
        self._filter_timer.timeout.connect(self.refresh_models)
        self._icons_timer = QTimer(self)
        self._icons_timer.setSingleShot(True)
        self._icons_timer.setInterval(60)
        self._icons_timer.timeout.connect(self._load_visible_icons)
        self._key_timer = QTimer(self)
        self._key_timer.setInterval(30)
        self._key_timer.timeout.connect(self._poll_keys)
        self._slow_timer = QTimer(self)
        self._slow_timer.setInterval(100)
        self._slow_timer.timeout.connect(self._tick)
        self._cam_timer = QTimer(self)
        self._cam_timer.setTimerType(Qt.PreciseTimer)
        self._cam_timer.setInterval(16)
        self._cam_timer.timeout.connect(self._cam_tick)

        self._reroll()
        self.refresh_models()
        self.refresh_placed()
        self.refresh_layer_list()
        self._update_hint()
        self._update_selection_ui()

    # ------------------------------------------------------------ включение
    def start(self) -> None:
        """Начать следить за Buldozer и клавишами (окно само не показывается — F8)."""
        self._slow_timer.start()
        if wb.IS_WINDOWS:
            self._key_timer.start()
            self._cam_timer.start()
        log.info("overlay watcher started (windows=%s)", wb.IS_WINDOWS)

    def stop(self) -> None:
        if self._layers_pool is not None:
            self._layers_pool.shutdown(wait=False, cancel_futures=True)
            self._layers_pool = None
            self._layers_job = None
        self._cam_off()
        self._slow_timer.stop()
        self._key_timer.stop()
        self._cam_timer.stop()
        self._wanted = False
        self.window.hide()

    def is_running(self) -> bool:
        return self._slow_timer.isActive()

    def toggle(self) -> None:
        self.set_wanted(not self._wanted)

    def set_wanted(self, on: bool) -> None:
        self._wanted = bool(on)
        self._dirty = True                # призрак у курсора показываем, только пока окно нужно
        if self._wanted:
            self._cam_disabled = ""       # новая попытка своей камеры
        if self._wanted:
            self.refresh_models()
        self._apply_visibility(force=True)

    def _load_keys(self, s: dict) -> None:
        self._k_toggle = wb.HotkeyEdge(wb.parse_hotkey(s.get("overlay_toggle_key") or "F8"))
        self._k_place = wb.HotkeyEdge(wb.parse_hotkey(s.get("overlay_place_key") or "F9"))
        self._k_undo = wb.HotkeyEdge(wb.parse_hotkey(s.get("overlay_undo_key") or "Shift+F9"))
        self._mouse_adjust = bool(s.get("overlay_mouse_adjust", True))
        self._rotate_hk = wb.parse_hotkey(s.get("overlay_rotate_key") or DEFAULT_ROTATE_KEY)
        self._lift_hk = wb.parse_hotkey(s.get("overlay_lift_key") or DEFAULT_LIFT_KEY)
        self._move_hk = wb.parse_hotkey(s.get("overlay_move_key") or DEFAULT_MOVE_KEY)
        self._k_select = wb.HotkeyEdge(wb.parse_hotkey(s.get("overlay_select_key") or DEFAULT_SELECT_KEY))
        self._k_delete = wb.HotkeyEdge(wb.parse_hotkey(s.get("overlay_delete_key") or DEFAULT_DELETE_KEY))
        self._k_escape = wb.HotkeyEdge(wb.parse_hotkey("Esc"))
        self._lock_cursor = bool(s.get("overlay_lock_cursor", True))
        self._yaw_per_px = _positive(s.get("overlay_mouse_yaw_per_px"), 0.5)
        self._lift_per_px = _positive(s.get("overlay_mouse_lift_per_px"), 0.01)
        self._cam_enabled = bool(s.get("overlay_camera", True))
        self._guard_on = bool(s.get("overlay_cursor_guard", True))
        self._jump_targets = []           # точки возврата игры (смещение от центра окна Buldozer)
        for t in s.get("overlay_cursor_targets") or []:
            if isinstance(t, (list, tuple)) and len(t) == 2:
                self._jump_targets.append((int(t[0]), int(t[1])))
        self._cam_vks = {}
        for name, key, default in CAMERA_KEYS:
            hk = wb.parse_hotkey(s.get(key) or default) or wb.parse_hotkey(default)
            self._cam_vks[name] = hk.vk
        look = wb.parse_hotkey(s.get("overlay_cam_look_key") or DEFAULT_LOOK_KEY) or \
            wb.parse_hotkey(DEFAULT_LOOK_KEY)
        self._cam_look_vk = look.vk
        self.camera.configure(speed=_positive(s.get("overlay_cam_speed"), 30.0),
                              sens=_positive(s.get("overlay_cam_sens"), 0.12),
                              fast_mult=_positive(s.get("overlay_cam_fast_mult"), 4.0),
                              invert_y=bool(s.get("overlay_cam_invert_y", False)),
                              along_view=bool(s.get("overlay_cam_fly_view", False)))
        if self._hook is not None:
            self._hook.swallow = self._cam_swallow()
            self._hook.look_vk = self._cam_look_vk

    def key_texts(self) -> tuple[str, str, str]:
        def t(edge, default):
            return edge.hotkey.text() if edge.hotkey else default
        return t(self._k_toggle, "F8"), t(self._k_place, "F9"), t(self._k_undo, "Shift+F9")

    def adjust_texts(self) -> tuple[str, str]:
        r, l = self._rotate_hk, self._lift_hk
        return (r.text() if r else DEFAULT_ROTATE_KEY), (l.text() if l else DEFAULT_LIFT_KEY)

    def select_texts(self) -> tuple[str, str]:
        k, m = self._k_select.hotkey, self._move_hk
        return (k.text() if k else DEFAULT_SELECT_KEY), (m.text() if m else DEFAULT_MOVE_KEY)

    def reload_settings(self) -> None:
        self._load_keys(load_settings())
        self._update_hint()
        self._dirty = True

    # ------------------------------------------------------------ таймеры
    def _poll_keys(self) -> None:
        try:
            self._handle_mouse()
            self._handle_keys()
        except Exception as exc:          # ошибка не должна молча глушить клавиши
            log.exception("overlay key poll failed")
            self._say(f"Ошибка обработки клавиш: {exc!r}")

    def _handle_mouse(self) -> None:
        """Держат сочетание поворота (ПКМ) или подъёма (Alt+ПКМ) в Buldozer и двигают мышь.

        Пока держат, модель **закреплена** там, где была в момент нажатия (скрипт получает
        точку в строке G), а курсор возвращается на место — после отпускания модель не прыгает,
        и F9 ставит её именно туда. Buldozer кнопку тоже получает (перехватить её снаружи
        нельзя). Начать можно только когда активен Buldozer; дальше — пока сочетание держат.
        """
        mode = None
        if self._mouse_adjust and self._wanted and self._bz:
            combos = (("lift", self._lift_hk), ("rotate", self._rotate_hk))
            if self._sel is not None:
                combos = (("move", self._move_hk),) + combos
            mode = wb.hold_match(combos, wb.key_down)
        if mode is None:
            self._end_drag()
            return
        pos = wb.cursor_pos()
        if pos is None:
            return
        if self._drag is not None and mode != self._drag_mode and "move" in (mode, self._drag_mode):
            self._end_drag()              # перенос ↔ поворот: закончить одно, начать другое
        if self._drag is None:
            if wb.foreground_is(self._bz):
                self._start_drag(pos, mode)
            return
        if mode == "move":
            return                        # объект везёт скрипт (строка D) — по точке под курсором
        self._drag_mode = mode            # Alt отпустили/зажали на ходу — режим меняется
        if self._cam_mouse:
            return                        # обзор (ЛКМ): курсор стоит, сдвиги отдаёт перехватчик камеры
        dx, dy = pos[0] - self._drag[0], pos[1] - self._drag[1]
        if not dx and not dy:
            return
        if abs(dx) + abs(dy) > DRAG_JUMP_PX and self._is_return_point(pos):
            # курсор переставила игра (в центр), а не рука — модель не крутить и не поднимать
            log.debug("adjust %s: cursor jump %s -> %s ignored (game return point)", mode, self._drag, pos)
            if self._warp and wb.set_cursor_pos(*self._drag):
                return
            self._drag = pos
            return
        if self._warp and not wb.set_cursor_pos(*self._drag):
            self._warp = False            # Windows не дала (Buldozer от администратора?) — без возврата
        if not self._warp:
            self._drag = pos
        self._adjust(mode, dx, dy)

    def _adjust(self, mode: str, dx: int, dy: int) -> None:
        """Поворот (мышь ↔) или подъём (мышь ↕) выбранной модели (или выделенного объекта)."""
        if mode == "move":
            return
        if self._sel is not None:
            self._adjust_selection(mode, dx, dy)
            return
        w = self.window
        if mode == "lift":
            if not dy:
                return
            value = w.lift.value() - dy * self._lift_per_px          # мышь вверх — выше
            self._set_quiet(w.lift, max(w.lift.minimum(), min(w.lift.maximum(), value)))
        else:
            if not dx:
                return
            if w.random_yaw.isChecked():
                w.random_yaw.blockSignals(True)
                w.random_yaw.setChecked(False)
                w.random_yaw.blockSignals(False)
            self._set_quiet(w.yaw, round((w.yaw.value() + dx * self._yaw_per_px) % 360.0, 1) % 360.0)
        self._reroll()
        self._dirty = True               # файл команд перепишет такт (10 раз в секунду)

    def _start_drag(self, pos: tuple[int, int], mode: str) -> None:
        self._drag = pos
        self._drag_mode = mode
        self._warp = self._lock_cursor and not self._cam_mouse and mode != "move"
        st = self._state
        self._pin = st.hit if (self._connected and st is not None and st.hit is not None
                               and self._sel is None) else None
        if self._sel is not None:
            self.session.begin_change()           # «Отменить» вернёт объект как был
            self._dirty = True
            self._write_commands()                # D — сразу, не дожидаясь такта
        log.debug("adjust %s start at %s pin=%s warp=%s", mode, pos, self._pin, self._warp)
        if self._pin is not None:
            self._dirty = True
            self._write_commands()        # закрепить сразу, не дожидаясь такта
        self._update_status()

    def _end_drag(self) -> None:
        if self._drag is None:
            return
        anchor, warp, mode = self._drag, self._warp, self._drag_mode
        self._drag = self._drag_mode = None
        self._warp = False
        if self._sel is not None:
            if mode == "move":
                self._finish_move()
            else:
                self.session.save()
                if self._sel.own >= 0:
                    self.refresh_placed()
            self._dirty = True
            self._write_commands()
            self._update_selection_ui()
        if warp:
            wb.set_cursor_pos(*anchor)    # на всякий случай: курсор точно там, где модель
        if self._pin is not None:
            self._pin = None
            self._dirty = True
            self._write_commands()
        self._update_status()

    @staticmethod
    def _set_quiet(spin, value: float) -> None:
        spin.blockSignals(True)
        spin.setValue(value)
        spin.blockSignals(False)

    def _handle_keys(self) -> None:
        if self._k_escape.update(self._key_down) and self._sel is not None and self._bz \
                and wb.foreground_is(self._bz):
            self.set_selection(None)
        if self._k_select.update(wb.key_down) and self._wanted and self._bz \
                and wb.foreground_is(self._bz):
            self.select_at_cursor()
        edges = (self._k_toggle, self._k_place, self._k_undo, self._k_delete)
        fired = [edge.update(self._key_down) for edge in edges]
        pressed = [edge for edge in edges if edge.pressed]
        if not pressed:
            return
        bz_active = wb.foreground_is(self._bz) if self._bz else False
        ov_active = self.window.isVisible() and wb.foreground_is_ours() and self.window.isActiveWindow()
        toggle, place, undo, delete = fired
        log.log(logging.INFO if self._k_toggle in pressed else logging.DEBUG,
                "overlay keys pressed=%s fired=%s bz=%s ov=%s wanted=%s mods=%s fg=%r",
                [e.hotkey.text() for e in pressed], fired, bz_active, ov_active, self._wanted,
                pressed[0].mods, wb.window_title(wb.foreground_window())[:40])
        if not (bz_active or ov_active):
            if self._wanted and self.window.isVisible():
                self._say(self._inactive_reason(pressed[0]))
            return
        if toggle:
            self.toggle()
        elif place and self._wanted:
            self.place_at_cursor()
        elif undo and self._wanted:
            self.undo()
        elif delete:
            if self._wanted and bz_active:        # в нашем окне Delete — для полей ввода и списка
                self.delete_selection()
        elif self._wanted and not any(fired) and pressed[0].hotkey != self._k_select.hotkey:
            ctrl, shift, alt = pressed[0].mods
            held = [n for n, on in (("Ctrl", ctrl), ("Shift", shift), ("Alt", alt)) if on]
            self._say(f"{pressed[0].hotkey.text()}: сочетание не совпало"
                      + (f" (зажато: {'+'.join(held)})" if held else ""))

    def _check_elevation(self, pid: int) -> None:
        """Buldozer от администратора, а программа нет — Windows не даёт ей клавиши и мышь."""
        self._bz_elevated = False
        if not pid:
            return
        bz_admin, we_admin = wb.process_elevated(pid), wb.self_elevated()
        log.info("overlay admin rights: buldozer=%s program=%s", bz_admin, we_admin)
        if bz_admin and not we_admin:
            self._bz_elevated = True
            log.warning("overlay: Buldozer runs as administrator, this program does not — Windows "
                        "hides keys/mouse of Buldozer from it; F8/F9 come from the bridge script (v15)")
            self._say(ELEVATED_TEXT)

    def _inactive_reason(self, edge) -> str:
        key = edge.hotkey.text() if edge.hotkey else "?"
        if not self._bz:
            return f"{key}: окно Buldozer не найдено"
        if self._bz_elevated:
            return f"{key}: {ELEVATED_TEXT}"
        fg = wb.foreground_window()
        title = wb.window_title(fg) or "без названия"
        return f"{key}: активно окно «{title[:40]}», а не Buldozer — кликните в Buldozer"

    def _tick(self) -> None:
        try:
            self._tick_inner()
        except Exception as exc:
            log.exception("overlay tick failed")
            self._say(f"Ошибка: {exc!r}")

    def _tick_inner(self) -> None:
        self._tick_no += 1
        if self._tick_no % 5 == 1 or (self._bz and not wb.is_window(self._bz)):
            bz = wb.find_buldozer_window() if wb.IS_WINDOWS else None
            if bz is None and self._bz and wb.is_window(self._bz) and not wb.is_minimized(self._bz):
                bz = self._bz                 # поиск разок не нашёл (заголовок/видимость) — окно то же
            if bz != self._bz:
                self._bz = bz
                pid = wb.window_pid(bz) if bz else 0
                exe = wb.process_exe(pid) if pid else ""
                self._bz_info = f"{exe.replace('/', chr(92)).rsplit(chr(92), 1)[-1] or 'pid'} {pid}" if bz else ""
                log.info("overlay buldozer window %s (%s)", bz, self._bz_info)
                self._check_elevation(pid)
        self._read_state()
        if self._dirty or self._written_dir != self._target_dir():
            self._write_commands()
        self._follow_buldozer()
        self._apply_visibility()
        self._layers_tick()
        if self.window.isVisible():
            self._update_status()

    # ------------------------------------------------------------ своя камера
    def _cam_swallow(self) -> set[int]:
        """Какие клавиши не отдавать Buldozer (модификаторы перехватчик пропускает сам).

        Esc — тоже: меню игры поверх своей камеры возвращает мышь игре (курсор — в центр);
        наше Esc (снять выделение) читается через перехватчик.
        """
        out = set(self._cam_vks.values()) | {wb.VK_ESCAPE}
        if self._k_delete.hotkey is not None:     # Delete — наше удаление, не Buldozer
            out.add(self._k_delete.hotkey.vk)
        return out

    def _cam_should_run(self) -> bool:
        # короткий перебой связи (файл состояния занят, Buldozer подвис на загрузке) камеру не
        # выключает: иначе игра забирает мышь и курсор прыгает в центр
        linked = self._connected or time.monotonic() - self._connected_at < CONNECT_GRACE
        return bool(self._cam_enabled and not self._cam_disabled and self._wanted and self._bz
                    and linked)

    def _key_down(self, vk: int) -> bool:
        """Клавиша нажата — и когда её глотает перехватчик своей камеры (тогда Windows не знает),
        и когда Windows её не показывает (Buldozer от администратора) — тогда по скрипту (KD|…)."""
        hook = self._hook
        return bool((hook is not None and hook.is_down(vk)) or wb.key_down(vk) or self._relay_down(vk))

    def _relay_down(self, vk: int) -> bool:
        """Клавиша нажата в Buldozer — по словам скрипта моста (v15, строка KD|…)."""
        st = self._state
        keys = getattr(st, "keys_down", None) if st is not None and self._connected else None
        if not keys:
            return False
        names = wb.VK_TO_KEYCODE.get(int(vk), ())
        return any(n in keys for n in names) and bool(self._bz) and wb.foreground_is(self._bz)

    def _relay_line(self) -> str:
        """KW|… — какие клавиши скрипту сообщать (горячие клавиши окна и модификаторы)."""
        vks = [wb.VK_SHIFT, wb.VK_CONTROL, wb.VK_MENU]
        for edge in (self._k_toggle, self._k_place, self._k_undo, self._k_escape, self._k_delete):
            if edge.hotkey is not None:
                vks.append(edge.hotkey.vk)
        return "KW|" + ",".join(wb.keycodes_for(vks))

    def _cam_watch_hook(self, hook) -> bool:
        """Windows молча снимает перехват, если он раз ответил слишком долго (> ~1 с). Курсор
        движется, а перехватчик ничего не видел — поставить заново. True — переставили."""
        pos = wb.cursor_pos()
        prev, self._hook_cursor = self._hook_cursor, pos
        seen = getattr(hook, "seen", None)
        if pos is None or prev is None or pos == prev or seen is None:
            return False
        if time.monotonic() - seen < HOOK_SILENCE:
            return False
        if any(abs(pos[0] - c[0]) <= GUARD_CENTER_PX and abs(pos[1] - c[1]) <= GUARD_CENTER_PX
               for c in self._centers()):
            return False                        # курсор поставили в центр — это игра, а не рука
        self._hook_restarts += 1
        log.warning("free camera: input hook stopped getting events (removed by Windows?) — "
                    "reinstalling (%d)", self._hook_restarts)
        hook.uninstall()
        if not hook.install():
            self._cam_shutdown("Windows сняла перехват клавиатуры и мыши и не дала поставить снова")
            return True
        hook.seen = time.monotonic()
        return True

    def _cam_tick(self) -> None:
        try:
            self._cam_tick_inner()
        except Exception as exc:          # камера не должна держать клавиши при ошибке
            log.exception("free camera tick failed")
            self._cam_shutdown(f"ошибка: {exc!r}")

    def _cam_tick_inner(self) -> None:
        now = time.monotonic()
        dt = 0.0 if self._cam_last is None else now - self._cam_last
        self._cam_last = now
        if self._cam_off_text and self._write_cam(self._cam_off_text):
            self._cam_off_text = ""
        if not self._cam_should_run():
            if self.camera.active or self._hook is not None:
                self._cam_off()
            return
        if not self.camera.active and not self._cam_on():
            return
        hook = self._hook
        if self._cam_watch_hook(hook):
            if self._hook is None:
                return
            hook = self._hook
        hook.target = self._bz
        keys = wb.foreground_is(self._bz)       # окно меню активно — печатать в поиске можно
        hook.capture_keys = keys
        hook.capture_wheel = keys
        mouse = keys and hook.look_held()       # зажата ЛКМ над Buldozer — обзор, курсор стоит
        hook.capture_mouse = mouse
        if (keys, mouse) != (self._cam_keys, self._cam_mouse):
            self._cam_keys, self._cam_mouse = keys, mouse
            self._update_status()
        dx, dy, wheel = hook.take()
        clicks = hook.take_clicks() if hasattr(hook, "take_clicks") else 0
        if mouse and not self._look_prev and self._wanted:
            self._gizmo_grab()                    # нажали ЛКМ на стрелке — тянуть её, не обзор
        self._look_prev = mouse
        if self._gizmo_drag is not None:
            clicks = 0
            if mouse:
                if dx or dy:
                    self._gizmo_move(dx, dy)
                dx = dy = 0                       # камера стоит
            else:
                self._gizmo_release()
        elif keys and not mouse:
            self._gizmo_hover()
        if clicks and keys and self._wanted and self._gizmo_hot is None:
            self.select_at_cursor()               # короткий щелчок ЛКМ над Buldozer — выделить
        if keys and not mouse and self._drag is None:
            self._cam_guard_cursor(hook, now)
        else:
            self._guard_pos = None              # обзор/поворот модели — курсор двигаем мы
        if not mouse:
            dx = dy = 0                         # обзор — только пока зажата кнопка
        if not keys:
            wheel = 0
        if self._drag is not None:              # держат поворот/подъём модели — мышь у модели
            if dx or dy:
                self._adjust(self._drag_mode, dx, dy)
            dx = dy = 0
        v = self._cam_vks
        inp = (CamInput(*(hook.is_down(v[name]) for name, _k, _d in CAMERA_KEYS)) if keys
               else CamInput())
        pointer = wb.pointer_in(self._bz)      # курсор над Buldozer — луч «под курсором» (скрипт v6)
        self._write_cam(self.camera.update(dt, inp, dx, dy, wheel, pointer))
        if wheel:
            self._update_status()

    def _centers(self) -> list[tuple[int, int]]:
        out = []
        rect = wb.client_rect(self._bz) if self._bz else None
        if rect and rect[2] > 0 and rect[3] > 0:
            out.append((rect[0] + rect[2] // 2, rect[1] + rect[3] // 2))
        size = wb.screen_size()
        if size:
            out.append((size[0] // 2, size[1] // 2))
        return out

    def _client_center(self) -> tuple[int, int] | None:
        rect = wb.client_rect(self._bz) if self._bz else None
        if rect and rect[2] > 0 and rect[3] > 0:
            return rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
        return None

    def _is_return_point(self, pos: tuple[int, int]) -> bool:
        """Сюда игра ставит курсор: центр окна/экрана или точка, куда он уже прыгал сам."""
        if any(abs(pos[0] - c[0]) <= GUARD_CENTER_PX and abs(pos[1] - c[1]) <= GUARD_CENTER_PX
               for c in self._centers()):
            return True
        cc = self._client_center()
        if cc is None:
            return False
        rel = (pos[0] - cc[0], pos[1] - cc[1])
        return any(abs(rel[0] - t[0]) <= GUARD_TARGET_PX and abs(rel[1] - t[1]) <= GUARD_TARGET_PX
                   for t in self._jump_targets)

    def _learn_jump(self, pos: tuple[int, int]) -> None:
        """Прыжок без руки: одна и та же точка (от центра окна) дважды — запомнить как точку
        возврата (и в настройках: в следующий раз сторож узнает её с первого прыжка)."""
        cc = self._client_center()
        if cc is None:
            return
        rel = (pos[0] - cc[0], pos[1] - cc[1])
        key = next((k for k in self._jump_seen if abs(k[0] - rel[0]) <= GUARD_TARGET_PX
                    and abs(k[1] - rel[1]) <= GUARD_TARGET_PX), rel)
        self._jump_seen[key] = self._jump_seen.get(key, 0) + 1
        if self._jump_seen[key] < GUARD_LEARN or self._is_return_point(pos):
            return
        self._jump_targets = (self._jump_targets + [key])[-GUARD_MAX_TARGETS:]
        log.info("cursor guard: learned return point %s from window centre", key)
        s = load_settings()
        s["overlay_cursor_targets"] = [list(t) for t in self._jump_targets]
        save_settings(s)

    def _cam_guard_cursor(self, hook, now: float) -> None:
        """Сторож курсора: игра/Buldozer «через время» ставят курсор в центр (SetCursorPos —
        перехватчик этого не видит). Мышь не двигали, а курсор прыгнул в центр окна — вернуть,
        где был. Каждый прыжок — в журнал (сколько, куда, через сколько после прошлого)."""
        pos = wb.cursor_pos()
        if pos is None:
            return
        moved = getattr(hook, "last_move", None)
        if moved is not None and moved != self._guard_move:     # мышь двигали — курсор там
            self._guard_move = moved
            self._guard_pos = moved
        prev = self._guard_pos
        if prev is None or pos == prev:
            self._guard_pos = pos
            return
        move_at = getattr(hook, "move_at", None)
        quiet = move_at is None or now - move_at > GUARD_QUIET
        jump = abs(pos[0] - prev[0]) + abs(pos[1] - prev[1])
        if not quiet:
            return                              # мышь ещё движется — подождём событий
        if jump <= GUARD_JUMP_PX:
            self._guard_pos = pos               # мелочь (край экрана, ClipCursor) — принять
            return
        self._learn_jump(pos)
        centered = self._is_return_point(pos)
        self._cam_jumps += 1
        last = getattr(self, "_guard_last_jump", None)
        self._guard_last_jump = now
        fix = centered and self._guard_on and now >= self._guard_paused_until
        if self._cam_jumps <= 20 or self._cam_jumps % 100 == 0:
            log.info("cursor jump #%d: %s -> %s (%s), %s; %s", self._cam_jumps, prev,
                     pos, "return point" if centered else "not a return point",
                     f"{now - last:.2f}s after previous" if last is not None else "first",
                     "returned" if fix else "left")
        if not fix:
            self._guard_pos = pos
            return
        self._guard_fixes = [t for t in self._guard_fixes if now - t < GUARD_WINDOW] + [now]
        if len(self._guard_fixes) >= GUARD_MAX_FIXES:
            self._guard_paused_until = now + GUARD_PAUSE
            self._guard_fixes = []
            log.warning("cursor guard: game keeps centering the cursor (%d times in %.0fs) — "
                        "pausing for %.0fs", GUARD_MAX_FIXES, GUARD_WINDOW, GUARD_PAUSE)
        wb.set_cursor_pos(*prev)
        after = wb.cursor_pos() or prev
        self._guard_pos = after
        self._hook_cursor = after               # сторож перехвата: это сдвинули мы, не рука
        self._guard_fixed += 1

    def _cam_on(self) -> bool:
        st = self._state
        if st is None or st.camera is None or st.direction is None:
            return False
        if self._hook is None:
            hook = self._hook_factory(self._cam_swallow(), self._cam_look_vk)
            if not hook.install():
                self._cam_shutdown("Windows не дала перехватить клавиатуру и мышь")
                return False
            self._hook = hook
        self.camera.start(st.camera, st.direction)
        self._cam_started = time.monotonic()
        self._cam_off_text = ""
        log.info("free camera on at %s", self.camera.pose)
        self._update_status()
        return True

    def _cam_off(self) -> None:
        """Отдать камеру Buldozer и снять перехват."""
        if self._hook is not None:
            self._hook.uninstall()
            self._hook = None
        self._cam_keys = self._cam_mouse = False
        if not self.camera.active:
            return
        speed = round(self.camera.speed, 2)
        text = self.camera.stop()
        if not self._write_cam(text):
            self._cam_off_text = text           # повторим на следующем такте
        log.info("free camera off")
        s = load_settings()
        if s.get("overlay_cam_speed") != speed:  # скорость, выбранную колесом, — запомнить
            s["overlay_cam_speed"] = speed
            save_settings(s)
        self._update_status()

    def _cam_shutdown(self, reason: str) -> None:
        log.warning("free camera disabled: %s", reason)
        self._cam_disabled = reason
        self._cam_off()
        self._say(f"Своя камера выключена: {reason}")

    def _cam_feedback(self, st) -> None:
        """Ответ скрипта: включилась ли камера, туда ли смотрит."""
        if not self.camera.active:
            return
        if st.free_camera_failed:
            self._cam_shutdown("Buldozer не создал камеру (staticcamera)")
            return
        flags = (st.game_cursor, st.mouse_focus, st.cursor_menu, st.menu_hidden)
        if flags != self._cam_script_flags:
            self._cam_script_flags = flags
            log.info("free camera: DayZ cursor %s, mouse focus %s, invisible menu %s, game menu %s",
                     "shown" if st.game_cursor else "not shown by script",
                     "taken" if st.mouse_focus else "not taken",
                     "open" if st.cursor_menu else "closed",
                     "hidden" if st.menu_hidden else "-")
        retakes = getattr(st, "focus_retakes", 0)
        if retakes != self._cam_retakes:
            if retakes > self._cam_retakes:
                log.info("free camera: mouse was given back to the game, script took it again "
                         "(%d times this session)", retakes)
            self._cam_retakes = retakes
        note = self.camera.feedback(st.free_camera, st.camera, st.direction)
        if note:
            log.info("%s", note)
        if self.camera.mismatch:
            self._cam_shutdown("Buldozer не переключился на нашу камеру")
        elif (not self.camera.confirmed
              and time.monotonic() - self._cam_started > CAM_CONFIRM_TIMEOUT):
            self._cam_shutdown("скрипт в Buldozer не включил камеру — обновите мост (кнопка "
                               "«Записать» в стартовом окне) и нажмите F10 в Buldozer")

    def _write_cam(self, text: str) -> bool:
        folder = self._target_dir()
        if folder is None or not folder.is_dir():
            return False
        tmp = folder / (CAM_FILE + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8", newline="\n")
            os.replace(tmp, folder / CAM_FILE)
        except OSError:                   # скрипт как раз читает файл — следующая запись через 16 мс
            return False
        return True

    def camera_hint(self) -> str:
        names = {name: wb.vk_name(vk) for name, vk in self._cam_vks.items()}
        return (f"{wb.vk_name(self._cam_look_vk)} (держать) + мышь — обзор, "
                f"{names['forward']}{names['left']}{names['back']}{names['right']} — движение, "
                f"{names['up']}/{names['down']} — вверх/вниз, {names['fast']} — быстрее, колесо — "
                f"скорость")

    # ------------------------------------------------------------ мост: файлы
    def _candidates(self) -> list[Path]:
        return profile_candidates(load_settings())

    def _target_dir(self) -> Path | None:
        """Куда писать команды: папка со свежим состоянием, иначе первая существующая."""
        if self._state_dir is not None:
            return self._state_dir
        for c in self._candidates():
            if c.is_dir():
                return c
        return None

    def _read_state(self) -> None:
        folder, age = freshest_state(self._candidates())
        self._connected = folder is not None and age is not None and age <= STATE_MAX_AGE
        if self._connected:
            self._connected_at = time.monotonic()
        if folder is not None:
            self._state_dir = folder
        if not self._connected:
            return
        try:
            text = (folder / STATE_FILE).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        st = parse_state(text)
        if not st.complete:
            return
        self._state = st
        if self.session.apply_state(st):
            self.refresh_placed()
        self._tb_live_tick(st)
        self._handle_pick(st)
        self._script_counters(st)
        self._cam_feedback(st)

    def _script_counters(self, st) -> None:
        """Журнал (скрипт v12): игра закрывала наше меню (MO) / прятала курсор (CS) — после этого
        DayZ и ставит курсор в центр; время рядом с «cursor jump» в журнале покажет причину."""
        seen = getattr(self, "_seen_counters", None)
        now = (st.menu_opens, st.cursor_shows, st.canvas, getattr(st, "cam_read_fails", 0))
        if seen is None or now[2] != seen[2]:
            if now[2] is not None:
                log.info("selection frame (canvas): %s", "drawn" if now[2] else "not available")
        if seen is not None and seen[2] is not None and now[0] < seen[0]:
            seen = None                           # F10 — новый скрипт, счётчики с нуля
        if seen is not None:
            if now[0] > seen[0]:
                log.info("script: invisible menu reopened (closed by the game) — total %d", now[0])
            if now[1] > seen[1]:
                log.info("script: DayZ cursor shown again (hidden by the game) — total %d", now[1])
            if now[3] > seen[3] and (seen[3] == 0 or now[3] // 50 > seen[3] // 50):
                log.info("script: camera file was busy (window was replacing it) %d times — camera "
                         "kept (bridge v13)", now[3])
        self._seen_counters = now

    def _command_text(self) -> str:
        model = self.current_model()
        show_ghost = self.window.ghost.isChecked() and model and self._wanted and self._sel is None
        yaw, scale = self._next
        return format_commands(self._generation, self.session.objects,
                               model if show_ghost else "", yaw, scale, self.window.lift.value(),
                               self._pin if show_ghost else None, self._map_lines())

    def _map_lines(self) -> list[str]:
        """Строки команд для объектов карты (скрипт v10): выбор, правки, перенос, выделение."""
        lines = [f"K|{self._pick_seq}", self._relay_line()] + self.tblive.verify_lines(self.session.objects)
        sess = self._state.session if self._state is not None else 0
        if not sess:
            return lines
        lines += [e.command() for e in self.session.map_edits
                  if e.sess == sess and e.ref >= 0 and e.changed]
        sel = self._sel
        if sel is not None and sel.session == sess:
            if self._drag_mode == "move":
                lines.append(f"D|{sess}|{sel.ref}")
            lines.append(f"S|{sess}|{sel.ref}")
            if self._gizmo_hot is not None:
                lines.append(f"GA|{self._gizmo_hot + 1}")        # подсветить стрелку
        return lines

    def _write_commands(self) -> None:
        folder = self._target_dir()
        if folder is None or not folder.is_dir():
            return
        if self._dirty:
            self._generation += 1
        tmp = folder / (CMD_FILE + ".tmp")
        try:
            tmp.write_text(self._command_text(), encoding="utf-8", newline="\n")
            os.replace(tmp, folder / CMD_FILE)
        except OSError as exc:       # скрипт как раз читает файл — повторим на следующем такте
            log.debug("command write failed: %s", exc)
            return
        self._dirty = False
        self._written_dir = folder

    def _changed(self) -> None:
        self._dirty = True
        self._write_commands()

    # ------------------------------------------------------------ вставка в открытый TB
    def _tb_folder(self) -> Path | None:
        d = self._layers_dir_quiet()
        return d.parent if d is not None else None

    def _tb_origin(self) -> tuple[float, float] | None:
        d = self._layers_dir_quiet()
        project = load_project(d) if d is not None else None
        if project is None:
            project = load_project(load_settings().get("project_dir") or "")
        return project.origin if project is not None else None

    def _tb_template(self, model: str) -> str | None:
        tpl = self.catalog.by_model(model)
        return tpl.name if tpl is not None else None

    def _tb_live_tick(self, st) -> None:
        if not self.session.objects:
            return
        before = self.tblive.message
        if self.tblive.step(self.session.objects, st, self.catalog.tb_names, self._tb_origin,
                            self._tb_template):
            self.session.save()
            self.refresh_placed()
            self._changed()
            self._update_layers_status()
        elif self.tblive.message != before:
            self._update_layers_status()

    # ------------------------------------------------------------ окно Buldozer
    def _follow_buldozer(self) -> None:
        rect = None
        if self._bz and not wb.is_minimized(self._bz):
            native = wb.client_rect(self._bz)
            if native and native[2] > 50 and native[3] > 50:
                rect = native_rect_to_logical(native)
        if rect == self._bz_rect:
            return
        self._bz_rect = rect
        if rect is not None and self.window.isVisible():
            self._place_window()

    def _place_window(self) -> None:
        r = self._bz_rect
        if r is None:
            return
        w = self.window
        if self._offset is None:
            h = min(w.height(), max(320, r.height() - 24))
            w.resize(w.width(), h)
            self._offset = QPoint(max(0, r.width() - w.width() - 12), 12)
        w.move_programmatically(r.topLeft() + self._offset)

    def _remember_offset(self) -> None:
        if self._bz_rect is not None:
            self._offset = self.window.pos() - self._bz_rect.topLeft()

    def _apply_visibility(self, force: bool = False) -> None:
        w = self.window
        show = self._wanted
        if show and self._bz and load_settings().get("overlay_autohide", True):
            show = wb.foreground_is(self._bz) or wb.foreground_is_ours()
        if show and not w.isVisible():
            self.refresh_models()         # список моделей мог обновиться в главном окне
            w.show()
            self._place_window()
            self._update_status()
            self._icons_timer.start()
        elif not show and w.isVisible():
            w.hide()
        if force:
            self._write_commands()

    def _on_closed(self) -> None:
        self._wanted = False
        self._dirty = True
        self._write_commands()

    # ------------------------------------------------------------ библиотека
    def set_catalog(self, catalog: TemplateCatalog) -> None:
        self.catalog = catalog
        self._filled_for = None
        self.refresh_models()

    def set_work_drive(self, work_drive: str) -> None:
        if work_drive != self.work_drive:
            self.work_drive = work_drive
            self.thumbs.set_work_drive(work_drive)
            self._filled_for = None
            self.refresh_models()

    def set_icon_size(self, key: str) -> None:
        if key not in ICON_SIZES:
            return
        self._icon_size = key
        self.window.set_icon_size(key)
        s = load_settings()
        if s.get("overlay_icon_size") != key:
            s["overlay_icon_size"] = key
            save_settings(s)
        self._icons_timer.start()

    def _fill_library_combo(self) -> None:
        combo = self.window.library
        names = self.catalog.library_names()
        current = self._library_filter
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(f"{ALL_LIBRARIES} ({len(names)})" if names else ALL_LIBRARIES, "")
        for name in names:
            combo.addItem(name, name)
        idx = combo.findData(current) if current in names else 0
        combo.setCurrentIndex(max(0, idx))
        combo.blockSignals(False)
        if current not in names:
            self._library_filter = ""

    def _on_library_changed(self, _index: int) -> None:
        self._library_filter = self.window.library.currentData() or ""
        s = load_settings()
        s["overlay_library"] = self._library_filter
        save_settings(s)
        self.refresh_models()

    def refresh_models(self) -> None:
        w = self.window
        text = w.filter.text().strip()
        key = (text.lower(), self._library_filter, self.catalog.revision, self.work_drive)
        if key == self._filled_for:
            return
        if self._filled_for is None or self._filled_for[2] != self.catalog.revision:
            self._fill_library_combo()
            key = (text.lower(), self._library_filter, self.catalog.revision, self.work_drive)
        self._filled_for = key
        current = self.current_model()
        found = self.catalog.filtered(text, self._library_filter)
        shown = found[:MAX_LIST]
        w.models.blockSignals(True)
        w.models.clear()
        self._items_by_model = {}
        for t in shown:
            item = QListWidgetItem(self._placeholder, t.name)
            item.setData(Qt.UserRole, t.file)
            item.setToolTip(f"{t.name}\n{t.file}\nбиблиотека: {t.library}")
            w.models.addItem(item)
            self._items_by_model.setdefault(t.key, []).append(item)
            if current and t.key == model_key(current):
                w.models.setCurrentItem(item)
        w.models.blockSignals(False)
        if not self.catalog.templates:
            where = self.catalog.project_dir or "не указана"
            w.models_count.setText(f"Нет моделей: в папке проекта ({where}) не найдено "
                                   "TemplateLibs\\*.tml. Укажите папку проекта в главном окне.")
        elif len(found) > len(shown):
            w.models_count.setText(f"Показано {len(shown)} из {len(found)} — уточните поиск.")
        else:
            w.models_count.setText(f"Моделей: {len(found)} из {len(self.catalog.templates)}")
        self._icons_timer.start()
        if self.current_model() != current:
            self._on_params()

    def current_model(self) -> str:
        item = self.window.models.currentItem()
        return item.data(Qt.UserRole) if item is not None else ""

    # ------------------------------------------------------------ превью
    def _model_path(self, model: str) -> Path:
        return model_file(self.work_drive, model)

    def _load_visible_icons(self) -> None:
        """Превью для видимых строк: из памяти/кэша сразу, остальное — в фоновую очередь."""
        lw = self.window.models
        if not lw.isVisible() and not self.window.isVisible():
            return
        view = lw.viewport().rect()
        first = lw.indexAt(view.topLeft())
        start = first.row() if first.isValid() else 0
        requested = []
        for row in range(start, lw.count()):
            item = lw.item(row)
            rect = lw.visualItemRect(item)
            if rect.top() > view.bottom():
                break
            if not rect.intersects(view):
                continue
            requested.append(item)
        for item in reversed(requested):      # очередь LIFO: верхние строки рисуются первыми
            self._apply_icon(item)

    def _apply_icon(self, item: QListWidgetItem) -> None:
        model = item.data(Qt.UserRole)
        path = self._model_path(model)
        pm = self.thumbs.pixmap(model, path)
        if pm is not None:
            item.setIcon(QIcon(pm))
        elif self.thumbs.known(path):
            item.setIcon(self._placeholder)

    def _on_thumb_ready(self, model: str) -> None:
        for item in self._items_by_model.get(model_key(model), []):
            self._apply_icon(item)

    def clear_thumbnails(self) -> int:
        n = self.thumbs.cache.clear()
        self.thumbs.forget()
        for items in self._items_by_model.values():
            for item in items:
                item.setIcon(self._placeholder)
        self._icons_timer.start()
        return n

    # ------------------------------------------------------------ расстановка
    def _reroll(self) -> None:
        w = self.window
        self._next = placement_values(w.yaw.value(), w.random_yaw.isChecked(), w.scale.value(),
                                      w.scale_jitter.value())

    def _on_params(self) -> None:
        self._reroll()
        s = load_settings()
        if bool(s.get("overlay_ghost", True)) != self.window.ghost.isChecked():
            s["overlay_ghost"] = self.window.ghost.isChecked()
            save_settings(s)
        self._changed()

    def place_at_cursor(self) -> bool:
        model = self.current_model()
        if not model:
            return self._say("Выберите модель в списке.")
        st = self._state
        if not self._connected or st is None:
            return self._say("Нет связи с Buldozer: нажмите F10 в Buldozer (скрипт моста).")
        point = self._pin or st.hit          # во время поворота/подъёма — где модель закреплена
        if point is None:
            return self._say("Под курсором нет земли: наведите курсор на рельеф в Buldozer.")
        x, ground, z = point
        yaw, scale = self._next
        obj = self.session.add(model, x, z, ground, yaw, scale, self.window.lift.value())
        log.info("overlay place #%d %s at (%.2f, %.2f) yaw=%.1f scale=%.2f", obj.id, model, x, z, yaw, scale)
        self._reroll()
        self.refresh_placed(select_last=True)
        if not self.window.multi.isChecked():
            # одна модель — сняли с курсора (призрак исчезает); «Множество объектов» — остаётся
            models = self.window.models
            models.blockSignals(True)
            models.setCurrentItem(None)
            models.clearSelection()
            models.blockSignals(False)
            self._pin = None
        self._changed()
        self._say(f"Поставлен #{obj.id}: X {x:.1f}  Z {z:.1f}")
        return True

    def undo(self) -> None:
        before = {o.id: o for o in self.session.objects}
        if self.session.undo():
            after = {o.id for o in self.session.objects}
            self._warn_in_tb([o for i, o in before.items() if i not in after and o.tb == "ok"])
            self._reattach_selection()
            self.refresh_placed()
            self._changed()
            self._say("Отменено.")
        else:
            self._say("Отменять нечего.")

    def delete_selected(self) -> None:
        ids = [it.data(Qt.UserRole) for it in self.window.placed.selectedItems()]
        in_tb = [o for o in self.session.objects if o.id in ids and o.tb == "ok"]
        if ids and self.session.remove(ids):
            self.refresh_placed()
            self._changed()
            self._warn_in_tb(in_tb)

    def _reattach_selection(self) -> None:
        """После «Отменить» правки объектов карты — новые объекты: выделение — на них."""
        sel = self._sel
        if sel is None:
            return
        if sel.is_map:
            e = next((e for e in self.session.map_edits if e.sess == sel.session and e.ref == sel.ref
                      and not e.deleted), None)
            if e is None and not sel.edit.changed and not sel.edit.deleted:
                self.session.map_edits.append(sel.edit)     # выделили позже — не правка, оставить
                self._update_selection_ui()
            elif e is None:
                self.set_selection(None)
            else:
                sel.edit = e
                self._update_selection_ui()
        elif self.session.get(sel.own) is None:
            self.set_selection(None)

    def delete_selection(self) -> bool:
        """Delete: удалить выделенный объект — свой (из сеанса) или карты (правка «удалён»:
        в Buldozer прячется, из слоя рабочей копии убирается). «Отменить» возвращает."""
        sel = self._sel
        if sel is None:
            return self._say("Delete: ничего не выделено (выделите объект щелчком ЛКМ).")
        name = _short(sel.model)
        if sel.own >= 0:
            obj = self.session.get(sel.own)
            self.set_selection(None)
            if obj is None or not self.session.remove([obj.id]):
                return False
            self.refresh_placed()
            self._changed()
            if obj.tb == "ok":
                self._warn_in_tb([obj])
            log.info("overlay delete own #%d %s", obj.id, obj.model)
            return self._say(f"Удалён #{obj.id} {name}. Вернуть — «Отменить».")
        self.session.begin_change()
        sel.edit.deleted = True
        sel.edit.saved = False
        self.session.save()
        self.set_selection(None)
        self._changed()
        self._update_layers_button()
        log.info("overlay delete map object %s at (%.2f, %.2f)", sel.model, sel.edit.x0, sel.edit.z0)
        return self._say(f"Объект карты {name} удалён: в Buldozer спрятан, из слоя убирается в рабочей "
                         "копии (в проект — «Применить в проект»). Вернуть — «Отменить».")

    # ---- гизмо: стрелки осей у выделенного (скрипт v16), тянуть ЛКМ
    def _gizmo(self):
        st, sel = self._state, self._sel
        g = getattr(st, "gizmo", None) if (st is not None and self._connected) else None
        if g is None or sel is None or (g.session, g.ref) != (sel.session, sel.ref):
            return None
        return g

    def _sel_values(self) -> tuple[float, float, float] | None:
        sel = self._sel
        if sel is None:
            return None
        if sel.own >= 0:
            o = self.session.get(sel.own)
            return (o.x, o.z, o.lift) if o is not None else None
        e = sel.edit
        return e.x, e.z, e.lift

    def _set_sel_values(self, x: float, z: float, lift: float) -> None:
        sel = self._sel
        lift = max(-100.0, min(100.0, lift))
        if sel.own >= 0:
            o = self.session.get(sel.own)
            if o is not None:
                o.x, o.z, o.lift = x, z, lift
        else:
            e = sel.edit
            e.x, e.z, e.lift, e.saved = x, z, lift, False

    def _gizmo_hover(self) -> None:
        axis = None
        if self._sel is not None and self._bz:
            axis = axis_at(self._gizmo(), wb.client_rect(self._bz), wb.cursor_pos())
        if axis != self._gizmo_hot:
            self._gizmo_hot = axis
            self._changed()                       # GA| — подсветка стрелки

    def _gizmo_grab(self) -> bool:
        g = self._gizmo()
        if g is None or not self._bz:
            return False
        rect = wb.client_rect(self._bz)
        axis = axis_at(g, rect, wb.cursor_pos())
        vals = self._sel_values()
        if axis is None or vals is None:
            return False
        drag = AxisDrag.begin(g, rect, axis, vals)
        if drag is None:
            return False
        self.session.begin_change()
        self._gizmo_drag = drag
        self._gizmo_hot = axis
        self._changed()
        log.info("gizmo: drag along %s (%s)", AXES[axis], _short(self._sel.model))
        return True

    def _gizmo_move(self, dx: int, dy: int) -> None:
        d = self._gizmo_drag
        if self._sel is None:
            self._gizmo_drag = None
            return
        d.add(dx, dy)
        self._set_sel_values(*d.values())
        self._dirty = True
        now = time.monotonic()
        if now - self._gizmo_write_at >= GIZMO_WRITE_EVERY:
            self._gizmo_write_at = now
            self._write_commands()
            self._update_selection_ui()

    def _gizmo_release(self) -> None:
        d, self._gizmo_drag = self._gizmo_drag, None
        if d is None:
            return
        self.session.save()
        if self._sel is not None and self._sel.own >= 0:
            self.refresh_placed()
        self._changed()
        self._update_selection_ui()
        self._update_status()
        log.info("gizmo: moved %.3f m along %s", d.meters, AXES[d.axis])

    def _warn_in_tb(self, objs) -> None:
        if objs:
            self._warn(f"Объекты уже вставлены в Terrain Builder ({len(objs)}): из списка убраны, "
                       "а из проекта их удалите в TB (или в Buldozer: выделить и D).")

    def clear_session(self, ask: bool = True) -> None:
        if not self.session.objects:
            return
        if ask and not dialogs.ask(
                self.window, "Очистить", f"Убрать из Buldozer и из списка все объекты сеанса "
                f"({len(self.session.objects)})?", default_yes=False):
            return
        self.session.clear()
        self.refresh_placed()
        self._changed()

    def refresh_placed(self, select_last: bool = False) -> None:
        w = self.window
        w.placed.clear()
        failed = 0
        for o in self.session.objects:
            name = o.model.replace("/", "\\").rsplit("\\", 1)[-1]
            mark = "  ⚠ не создан" if o.failed else TB_MARKS.get(o.tb, "")
            failed += o.failed
            item = QListWidgetItem(f"#{o.id}  {name}  ({o.x:.1f}, {o.z:.1f})  {o.yaw:.0f}°{mark}")
            item.setData(Qt.UserRole, o.id)
            item.setToolTip(o.model)
            w.placed.addItem(item)
        if select_last and w.placed.count():
            w.placed.setCurrentRow(w.placed.count() - 1)
            w.placed.scrollToBottom()
        w.placed_label.setText(f"Поставлено в этом сеансе: {len(self.session.objects)}")
        self._update_layers_button()
        if failed:
            self._warn(f"Buldozer не смог создать {failed} объект(ов): модель не найдена по "
                       f"указанному пути. Пути должны быть от корня рабочего диска (P:\\).")
        else:
            self._warn("")

    # ------------------------------------------------------------ объекты карты (скрипт v10)
    def select_at_cursor(self) -> bool:
        """Выделить объект под курсором (ответ скрипта придёт строкой P)."""
        if not self._connected or self._state is None:
            return self._say("Нет связи с Buldozer: нажмите F10 в Buldozer (скрипт моста).")
        self._pick_seq += 1
        self._pick_pending = (self._pick_seq, time.monotonic())
        self._dirty = True
        self._write_commands()
        return True

    def _handle_pick(self, st) -> None:
        pending = self._pick_pending
        if pending is None:
            return
        p = st.pick
        if p is None or p.seq != pending[0]:
            if time.monotonic() - pending[1] > PICK_TIMEOUT:
                self._pick_pending = None
                self._say("Скрипт не ответил на выделение — обновите мост (кнопка «Записать» в "
                          "стартовом окне, нужна v10) и нажмите F10 в Buldozer.")
            return
        self._pick_pending = None
        if not p.found:
            self.set_selection(None)
            self._say("Под курсором нет объекта.")
            return
        if p.own >= 0 and self.session.get(p.own) is not None:
            sel = Selection(p.session, p.ref, p.model, own=p.own)
        else:
            edit = self.session.edit_map_object(p, p.session)
            self.session.add_sample(p.model, p.x0, p.z0)
            sel = Selection(p.session, p.ref, p.model, edit=edit)
        log.info("overlay select %s ref=%d own=%d at (%.2f, %.2f)", p.model, p.ref, p.own,
                 p.pos[0], p.pos[2])
        self.set_selection(sel)

    def set_selection(self, sel: Selection | None) -> None:
        if self._drag is not None:
            self._end_drag()
        if self._gizmo_drag is not None:
            self._gizmo_release()
        self._gizmo_hot = None
        self._sel = sel
        self._dirty = True
        self._write_commands()
        self._update_selection_ui()
        self._update_status()

    def _on_model_changed(self) -> None:
        if self._sel is not None and self.current_model():
            self.set_selection(None)              # выбрали модель — снова расстановка
        self._on_params()

    def _adjust_selection(self, mode: str, dx: int, dy: int) -> None:
        sel = self._sel
        if sel.own >= 0:
            obj = self.session.get(sel.own)
            if obj is None:
                self.set_selection(None)
                return
            if mode == "lift" and dy:
                obj.lift = max(-100.0, min(100.0, obj.lift - dy * self._lift_per_px))
            elif mode == "rotate" and dx:
                obj.yaw = round((obj.yaw + dx * self._yaw_per_px) % 360.0, 2) % 360.0
            else:
                return
        else:
            e = sel.edit
            if mode == "lift" and dy:
                e.lift = max(-100.0, min(100.0, e.lift - dy * self._lift_per_px))
            elif mode == "rotate" and dx:
                e.yaw = round((e.yaw + dx * self._yaw_per_px) % 360.0, 2) % 360.0
            else:
                return
            e.saved = False
        self._dirty = True
        self._update_selection_ui()

    def _finish_move(self) -> None:
        """Отпустили перенос: взять, куда скрипт довёз объект (строка DP)."""
        sel, st = self._sel, self._state
        if sel is None or st is None or st.drag is None or st.drag[:2] != (sel.session, sel.ref):
            return
        x, z = st.drag[2], st.drag[3]
        if sel.own >= 0:
            obj = self.session.get(sel.own)
            if obj is not None:
                obj.x, obj.z = x, z
                self.session.save()
                self.refresh_placed()
        else:
            sel.edit.x, sel.edit.z = x, z
            sel.edit.saved = False
            self.session.save()

    def revert_selection(self) -> None:
        sel = self._sel
        if sel is None:
            return
        if not sel.is_map:
            self._say("Свой объект: верните его кнопкой «Отменить».")
            return
        e = sel.edit
        e.x, e.z, e.yaw, e.lift, e.saved = e.x0, e.z0, e.yaw0, 0.0, False
        self.session.save()
        self._dirty = True
        self._write_commands()
        self._update_selection_ui()

    def forget_map_edits(self) -> None:
        n = len(self.session.changed_map_edits())
        if n and not dialogs.ask(self.window, "Забыть правки",
                                 f"Вернуть на место в Buldozer и забыть правки объектов карты ({n})?",
                                 default_yes=False):
            return
        self.session.map_edits = []
        self.session.save()
        if self._sel is not None and self._sel.is_map:
            self._sel = None
        self._dirty = True
        self._write_commands()
        self._update_selection_ui()

    def _names_for_model(self, model: str) -> list[str]:
        key = norm_model(model)
        return [t.name for t in self.catalog.templates if norm_model(t.file) == key]

    def _layers_dir_quiet(self) -> Path | None:
        """Папка слоёв проекта без вопросов (из настроек или рядом с проектом)."""
        s = load_settings()
        saved = s.get("overlay_layers_dir")
        if saved and Path(saved).is_dir():
            return Path(saved)
        project = s.get("project_dir") or ""
        key = str(project)
        if self._layers_dir_cache[0] != key or self._layers_dir_cache[1] is None:
            self._layers_dir_cache = (key, find_layers_dir(project) if project else None)
        return self._layers_dir_cache[1]

    def _layers_dir(self) -> Path | None:
        found = self._layers_dir_quiet()
        if found is not None:
            return found
        s = load_settings()
        start = s.get("project_dir") or ""
        path = dialogs.pick_folder(self.window, "Папка слоёв проекта (<Проект>.Layers с файлами *.tv4l)",
                                   start)
        if not path:
            return None
        s["overlay_layers_dir"] = path
        save_settings(s)
        self.refresh_layer_list()
        return Path(path)

    def _save_flag(self, key: str, on: bool) -> None:
        s = load_settings()
        if s.get(key) != bool(on):
            s[key] = bool(on)
            save_settings(s)
        if key == "overlay_layers_auto":
            self._layers_written_sig = None       # включили — записать текущее состояние
            self._update_layers_status()
        if key == "overlay_tb_live":
            self.tblive.enabled = bool(on)
            self._update_layers_status()
        if key == "overlay_tb_save":
            self.tblive.save = bool(on)

    # ------------------------------------------------------------ слои TB (рабочая копия)
    def target_layer(self) -> str:
        name = self.window.layer.currentText()
        return name or load_settings().get("overlay_target_layer") or DEFAULT_TARGET_LAYER

    def refresh_layer_list(self) -> None:
        """Слои проекта в выпадающем списке «Слой TB» (как выбор активного слоя в TB)."""
        combo = self.window.layer
        names = layer_sync.layer_names(self._layers_dir_quiet())
        want = load_settings().get("overlay_target_layer") or ""
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(names)
        pick = next((i for i, n in enumerate(names) if n.lower() == want.lower()), -1)
        if pick < 0:
            pick = next((i for i, n in enumerate(names) if n.lower() == DEFAULT_TARGET_LAYER.lower()), 0)
        if names:
            combo.setCurrentIndex(pick)
        combo.setEnabled(bool(names))
        combo.blockSignals(False)
        self.layersChanged.emit()
        self._update_layers_status()
        layers_dir = self._layers_dir_quiet()
        if names and layers_dir is not None:     # копия оригиналов — пока файлы читаются
            if self._layers_pool is None:
                self._layers_pool = ThreadPoolExecutor(1, thread_name_prefix="we-layers")
            self._layers_pool.submit(self._prime_layers, layers_dir)

    @staticmethod
    def _prime_layers(layers_dir: Path) -> None:
        try:
            _layers, errors, cached = layer_sync.read_layers(layers_dir)
        except Exception:                          # noqa: BLE001 - фон: только в журнал
            log.exception("layers read failed")
            return
        if errors or cached:
            log.warning("layers read at start: errors %s; from copy %s", errors, cached)
        else:
            log.info("layers read at start: %d file(s), copies up to date", len(_layers))

    def set_target_layer(self, name: str) -> None:
        combo = self.window.layer
        i = combo.findText(name, Qt.MatchFixedString)
        if i >= 0 and i != combo.currentIndex():
            combo.setCurrentIndex(i)              # -> _on_target_layer

    def _on_target_layer(self, name: str) -> None:
        if not name:
            return
        s = load_settings()
        if s.get("overlay_target_layer") != name:
            s["overlay_target_layer"] = name
            save_settings(s)
            log.info("overlay target layer: %s", name)
        self._layers_written_sig = None           # новые объекты переезжают в другой слой
        self.layersChanged.emit()

    def _layers_signature(self) -> tuple:
        objs = tuple((o.id, o.model, round(o.x, 4), round(o.z, 4), round(o.yaw, 3), round(o.scale, 4),
                      round(o.lift, 4), None if o.tb_rel is None else round(o.tb_rel, 4), o.failed)
                     for o in self.session.objects)
        edits = tuple((e.model, e.x0, e.z0, e.yaw0, round(e.x, 4), round(e.z, 4), round(e.yaw, 3),
                       round(e.lift, 4), e.deleted) for e in self.session.map_edits if e.changed)
        return objs, edits, self.target_layer()

    def _layers_args(self, layers_dir: Path) -> dict:
        """Снимок всего нужного для сборки — в фоне сеанс не читаем (его меняет окно)."""
        s = load_settings()
        offset = s.get("overlay_layer_offset")
        offset = tuple(offset) if isinstance(offset, (list, tuple)) and len(offset) == 2 else None
        names = {}
        for e in self.session.map_edits:
            if e.changed and e.model not in names:
                names[e.model] = self._names_for_model(e.model)
        templates = {}
        for o in self.session.objects:
            if o.model not in templates:
                templates[o.model] = self.catalog.by_model(o.model)
        return dict(layers_dir=layers_dir, project_dir=s.get("project_dir") or "",
                    objects=[replace(o) for o in self.session.objects],
                    edits=[replace(e) for e in self.session.map_edits if e.changed],
                    target=self.target_layer(), templates=templates, names=names,
                    samples=list(self.session.samples), offset=offset)

    @staticmethod
    def _run_layers(args: dict) -> layer_sync.SyncReport:
        layers_dir = args["layers_dir"]
        project = load_project(layers_dir) or (load_project(args["project_dir"])
                                               if args["project_dir"] else None)
        templates, names = args["templates"], args["names"]
        return layer_sync.sync(layers_dir, args["objects"], args["edits"], args["target"],
                               templates.get, project, lambda m: names.get(m, ()), args["samples"],
                               offset=args["offset"])

    def _layers_tick(self) -> None:
        """Раз в такт: изменения улеглись — пересобрать рабочую копию в фоне."""
        job = self._layers_job
        if job is not None and job[0].done():
            self._layers_job = None
            try:
                rep = job[0].result()
            except Exception as exc:              # noqa: BLE001 - показать, а не упасть
                log.exception("overlay layers write failed")
                rep = layer_sync.SyncReport(error=f"ошибка записи слоёв: {exc!r}")
            self._layers_done(rep, job[1])
        if self._layers_job is not None or not self.window.layers_auto.isChecked():
            return
        sig = self._layers_signature()
        now = time.monotonic()
        if sig != self._layers_seen_sig:          # ещё меняют — ждём паузу
            self._layers_seen_sig = sig
            self._layers_due = now + LAYERS_WRITE_DELAY
            return
        if sig == self._layers_written_sig or now < self._layers_due or self._drag is not None:
            return
        if not sig[0] and not sig[1] and self._layers_written_sig is None and not self._has_work_copy():
            self._layers_written_sig = sig        # нечего писать и нечего убирать
            return
        layers_dir = self._layers_dir_quiet()
        if layers_dir is None:
            self._layers_written_sig = sig
            self._layers_report = layer_sync.SyncReport(error="не найдена папка слоёв проекта "
                                                              "(<Проект>.Layers)")
            self._update_layers_status()
            return
        if self._layers_pool is None:
            self._layers_pool = ThreadPoolExecutor(1, thread_name_prefix="we-layers")
        self._layers_job = (self._layers_pool.submit(self._run_layers, self._layers_args(layers_dir)), sig)

    def _has_work_copy(self) -> bool:
        d = self._layers_dir_quiet()
        return d is not None and any(layer_sync.work_dir(d).glob("*.tv4l"))

    def _layers_done(self, rep: layer_sync.SyncReport, sig) -> None:
        self._layers_written_sig = sig
        self._layers_report = rep
        log.info("overlay layers written: %s; files %s; removed %s; skipped %s; not found %d",
                 rep.message, [p.name for p in rep.written], [p.name for p in rep.removed],
                 rep.skipped[:5], len(rep.not_found))
        if rep.load_errors or rep.from_cache:
            log.warning("overlay layers read: errors %s; from copy (file busy) %s", rep.load_errors,
                        rep.from_cache)
        self._update_layers_status()
        self.layersChanged.emit()

    def _update_layers_status(self) -> None:
        w = self.window
        rep = self._layers_report
        if self._layers_dir_quiet() is None:
            text = "Слои TB: папка <Проект>.Layers не найдена (укажите папку проекта в стартовом окне)."
        elif rep is None:
            text = ("Слои TB: новые объекты пишутся в выбранный слой рабочей копии; в проект — "
                    "«Применить в проект» в стартовом окне при закрытом TB.")
            if not w.layers_auto.isChecked():
                text = "Слои TB: запись по кнопке «Записать в слои»."
        elif rep.error:
            text = f"Слои TB: {rep.error}"
        else:
            text = f"Слои TB (рабочая копия): {rep.message}"
            if rep.skipped:
                text += " — " + "; ".join(sorted({f"{_short(m)}: {why}" for m, why in rep.skipped})[:3])
            if rep.load_errors:
                text += "\nНе прочитаны: " + "; ".join(rep.load_errors[:3])
            if rep.from_cache:
                text += f"\n(заняты Terrain Builder — взяты копии: {', '.join(rep.from_cache)})"
        if rep is not None and rep.frame_warning:
            text += f"\n⚠ {rep.frame_warning}"
        live = self.tblive.status(self.session.objects)
        if w.tb_live.isChecked() and (live.ok or live.sent or live.failed or live.message):
            parts = [f"вставлено {live.ok}"]
            if live.sent:
                parts.append(f"ждём подтверждения {live.sent}")
            if live.failed:
                parts.append(f"не принято {live.failed}")
            text += "\nОткрытый TB: " + ", ".join(parts) + (" — сохраните проект в TB (Ctrl+S)"
                                                           if live.ok and not w.tb_save.isChecked() else "")
            if live.message:
                text += f"\n{live.message}"
        w.layers_status.setText(text)

    def layers_work_dir(self) -> Path | None:
        d = self._layers_dir_quiet()
        return layer_sync.work_dir(d) if d is not None else None

    def write_layers(self, parent=None, quiet: bool = False) -> layer_sync.SyncReport | None:
        """Записать рабочую копию сейчас (в окне — с ожиданием; для кнопок)."""
        parent = parent or self.window
        layers_dir = self._layers_dir()
        if layers_dir is None:
            self._say("Не найдена папка слоёв проекта (<Проект>.Layers).")
            return None
        if self._layers_job is not None:          # фоновая запись — дождаться, чтобы не столкнуться
            try:
                self._layers_job[0].result(timeout=30)
            except Exception:                     # noqa: BLE001 - запишем заново ниже
                pass
            self._layers_job = None
        sig = self._layers_signature()
        args = self._layers_args(layers_dir)
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            rep = self._run_layers(args)
        except Exception as exc:                  # noqa: BLE001 - сообщить, а не упасть
            log.exception("write layers failed")
            QGuiApplication.restoreOverrideCursor()
            dialogs.error(parent, "Слои", f"Не удалось записать слои:\n{exc!r}")
            return None
        QGuiApplication.restoreOverrideCursor()
        self._layers_done(rep, sig)
        for e in self.session.map_edits:
            if e.changed and not any(_same_edit(e, n) for n in rep.not_found) and rep.written:
                e.saved = True
        if rep.offset is not None and args["offset"] is None and rep.edits_project is None and rep.moved:
            s = load_settings()
            s["overlay_layer_offset"] = [round(rep.offset[0], 4), round(rep.offset[1], 4)]
            save_settings(s)
        self.session.save()
        self._update_selection_ui()
        if quiet:
            return rep
        work = layer_sync.work_dir(layers_dir)
        if rep.error and not rep.written:
            dialogs.error(parent, "Слои", _layers_error_text(rep))
            return rep
        if not rep.written:
            dialogs.info(parent, "Слои", "Записывать нечего: нет своих объектов и передвинутых "
                                         "объектов карты." + (f" ({rep.edits_message})" if rep.not_found else ""))
            return rep
        lines = [f"Записаны копии слоёв ({rep.added + rep.moved} объект(ов)) в папку:", str(work), ""]
        lines += [f"  {p.name}" for p in rep.written]
        if rep.added:
            lines += ["", f"Новых объектов в слое «{rep.target}»: {rep.added}."]
        if rep.moved:
            lines += [f"Передвинуто объектов карты: {rep.moved}" +
                      (f" (подъём — в поле Relative у {rep.lifted})" if rep.lifted else "") + "."]
        if rep.skipped:
            lines += ["", f"Пропущено: {len(rep.skipped)} — " +
                      "; ".join(sorted({f"{_short(m)}: {why}" for m, why in rep.skipped})[:5])]
        if rep.not_found:
            lines += ["", f"Не найдены в слоях: {len(rep.not_found)} — " +
                      ", ".join(sorted({_short(e.model) for e in rep.not_found}))]
        if rep.moved and rep.edits_project is None:
            lines += ["", "Проект *.tv4p не найден — сдвиг координат подобран по выделенным объектам "
                          "(точность ~0.3 м, без учёта центров моделей)."]
        lines += ["", "Чтобы перенести в проект: закройте Terrain Builder и нажмите «Применить в "
                      "проект…» в стартовом окне (заменяемые слои сохраняются в резервную копию)."]
        dialogs.info(parent, "Слои", "\n".join(lines))
        return rep

    def save_to_layers(self) -> None:
        self.write_layers()

    def apply_layers_to_project(self, parent=None) -> bool:
        """Перенести рабочую копию в слои проекта (TB закрыт; заменяемые файлы — в резервную копию)."""
        parent = parent or self.window
        layers_dir = self._layers_dir()
        if layers_dir is None:
            return False
        if layer_sync.tb_running():
            dialogs.error(parent, "Применить в проект",
                          "Terrain Builder запущен. Он держит слои в памяти и при сохранении проекта "
                          "перезапишет их — закройте Terrain Builder (сохранив свои изменения в нём) "
                          "и нажмите кнопку ещё раз.")
            return False
        rep = self.write_layers(parent, quiet=True)
        if rep is None:
            return False
        if rep.error and not rep.written:
            dialogs.error(parent, "Применить в проект", _layers_error_text(rep))
            return False
        work = layer_sync.work_dir(layers_dir)
        files = sorted(p.name for p in work.glob("*.tv4l"))
        if not files:
            dialogs.info(parent, "Применить в проект", "В рабочей копии нет изменённых слоёв — "
                                                      "нечего применять.")
            return False
        text = (f"Заменить в проекте слои: {', '.join(files)}?\n\nПапка слоёв: {layers_dir}\n"
                f"Сейчас там: {rep.message}.\nЗаменяемые файлы будут сохранены в "
                f"{layers_dir.parent / layer_sync.BACKUP_ROOT}.")
        if not dialogs.ask(parent, "Применить в проект", text, default_yes=False):
            return False
        try:
            done, backup = layer_sync.apply_to_project(work, layers_dir)
        except Exception as exc:                  # noqa: BLE001 - сообщить, а не упасть
            log.exception("apply layers failed")
            dialogs.error(parent, "Применить в проект", f"Не удалось заменить слои:\n{exc!r}")
            return False
        log.info("overlay layers applied to project: %s (backup %s)", [p.name for p in done], backup)
        n_obj, n_edit = len(self.session.objects), len(self.session.changed_map_edits())
        # объекты уже в проекте: в сеансе они бы добавились в слои второй раз
        self.session.clear()
        self.session.map_edits = []
        self.session.save()
        self._sel = None
        self.refresh_placed()
        self._changed()
        self._layers_written_sig = None
        for p in work.glob("*.tv4l"):
            try:
                p.unlink()
            except OSError:
                pass
        self._layers_report = None
        self._update_layers_status()
        self._update_selection_ui()
        self.layersChanged.emit()
        dialogs.info(parent, "Применить в проект",
                     f"Готово: заменены {', '.join(p.name for p in done)}.\n"
                     f"Резервная копия прежних слоёв: {backup}\n\n"
                     f"Сеанс очищен (объектов: {n_obj}, правок: {n_edit}) — они теперь в слоях проекта. "
                     "Откройте проект в Terrain Builder.")
        return True

    def _update_selection_ui(self) -> None:
        w = self.window
        sel = self._sel
        select_key, move_key = self.select_texts()
        if sel is None:
            w.sel_label.setText(f"Выделение: {select_key} или щелчок ЛКМ по объекту в Buldozer "
                                f"(и стоявшему на карте, и своему).")
        else:
            name = sel.model.replace("/", "\\").rsplit("\\", 1)[-1]
            rotate, lift = self.adjust_texts()
            if sel.is_map:
                e = sel.edit
                what = (f"объект карты · сдвиг {e.x - e.x0:+.2f}/{e.z - e.z0:+.2f} м, "
                        f"курс {e.yaw:.1f}°" + (f", подъём {e.lift:+.2f} м" if e.lift else ""))
            elif self.session.get(sel.own) is not None:
                o = self.session.get(sel.own)
                what = (f"свой #{sel.own} · X {o.x:.1f} Z {o.z:.1f}, курс {o.yaw:.1f}°"
                        + (f", подъём {o.lift:+.2f} м" if o.lift else ""))
            else:
                o = self.session.get(sel.own)
                what = (f"свой #{sel.own} · X {o.x:.1f} Z {o.z:.1f}, курс {o.yaw:.1f}°"
                        if o is not None else f"свой #{sel.own}")
            gizmo = "стрелки X/Y/Z — тянуть ЛКМ по оси, " if self._cam_enabled else ""
            delete = self._k_delete.hotkey.text() if self._k_delete.hotkey else DEFAULT_DELETE_KEY
            w.sel_label.setText(f"Выделено: {name} — {what}\n{gizmo}{move_key} — перемещать, {rotate} — "
                                f"поворот, {lift} — подъём; {delete} — удалить; Esc — снять выделение.")
        w.sel_reset.setEnabled(sel is not None and sel.is_map)
        w.sel_clear.setEnabled(sel is not None)
        w.edits_clear.setEnabled(bool(self.session.map_edits))
        self._update_layers_button()

    def _update_layers_button(self) -> None:
        n = len(self.session.changed_map_edits()) + len(self.session.objects)
        self.window.save_layers.setText(f"Записать в слои ({n})" if n else "Записать в слои")
        self.window.save_layers.setEnabled(bool(n) or self._has_work_copy())

    # ------------------------------------------------------------ экспорт / настройка
    def export_for_tb(self) -> None:
        if not self.session.objects:
            self._say("Экспортировать нечего: список пуст.")
            return
        settings = load_settings()
        folder = settings.get("last_export_dir") or settings.get("project_dir") or ""
        start = str(Path(folder) / "buldozer_objects.txt") if folder else "buldozer_objects.txt"
        path = dialogs.save_file(
            self.window, "Экспорт объектов для Terrain Builder", start,
            "Текст для импорта в Terrain Builder (*.txt);;Все файлы (*)")
        if not path:
            return
        settings = load_settings()
        settings["last_export_dir"] = str(Path(path).parent)
        save_settings(settings)
        template = bool(settings.get("overlay_template_names", True))
        relative = bool(settings.get("overlay_tb_relative_height", True))
        project = load_project(settings.get("project_dir") or "")
        offset = project.origin if project is not None else None
        try:
            n = self.session.export_tb(path, template, relative, names=self.catalog.tb_names(),
                                       offset=offset)
        except OSError as exc:
            dialogs.error(self.window, "Экспорт", f"Не удалось записать файл:\n{exc}")
            return
        log.info("overlay export %d objects -> %s; frame offset %s (%s)", n, path, offset,
                 project.path if project is not None else "no project")
        if project is not None:
            where = (f"Координаты — в рамке карты проекта {project.path.name}: X +{offset[0]:.2f}, "
                     f"Z +{offset[1]:.2f}.\n")
        else:
            where = ("Проект *.tv4p не найден в папке проекта — координаты как у DayZ Editor "
                     "(X +200000, Z +0, рамка TB по умолчанию). Если рамка карты в TB другая, "
                     "объекты встанут не там — укажите папку проекта в окне запуска.\n")
        answer = dialogs.ask(
            self.window, "Экспорт",
            f"Записано объектов: {n}\n{path}\n\n"
            "В Terrain Builder: File → Import → Objects…, формат как у DayZ Editor "
            "(\"имя\";X;Z;yaw;pitch;roll;scale;высота).\n" + where
            + ("Высота — над землёй (TB запишет её в Relative) с поправкой на перенос начала "
               "модели (autocenter), низ модели — как в Buldozer.\n\n"
               if relative else "Высота — абсолютная (старый вариант DayZ Editor).\n\n")
            +
            "После импорта TB сам покажет их в Buldozer. Убрать наши копии из Buldozer и "
            "очистить список, чтобы не было двойников?")
        if answer:
            self.clear_session(ask=False)

    # ------------------------------------------------------------ подписи
    def _say(self, text: str) -> bool:
        self._message = text
        self._update_status()
        return False

    def _warn(self, text: str) -> None:
        self.window.warn.setText(text)
        self.window.warn.setVisible(bool(text))

    def _update_hint(self) -> None:
        toggle, place, undo = self.key_texts()
        rotate, lift = self.adjust_texts()
        self.window.place.setText(f"Поставить под курсор ({place})")
        self.window.undo.setText(f"Отменить ({undo})")
        select, move = self.select_texts()
        mouse = (f"держать {rotate} + мышь влево/вправо — поворот, {lift} + мышь вверх/вниз — "
                 "подъём (модель при этом стоит на месте); " if self._mouse_adjust else "")
        mouse += (f"{select} (или щелчок ЛКМ) — выделить объект под курсором, {move} — "
                  "перемещать выделенный; ")
        camera = (f" Своя камера (пока окно открыто): {self.camera_hint()}."
                  if self._cam_enabled else "")
        self.window.hint.setText(
            f"{toggle} — показать/скрыть окно, {place} — поставить, {undo} — отменить; {mouse}"
            "всё — когда активен Buldozer. В Buldozer один раз нажмите F10. "
            "ПКМ/СКМ — правая/средняя кнопка мыши." + camera)

    def _update_status(self) -> None:
        parts = []
        if wb.IS_WINDOWS:
            parts.append(f"Buldozer: найден ({self._bz_info})" if self._bz else "Buldozer: окно не найдено")
        if self._connected and self._state is not None:
            parts.append(f"Связь со скриптом: есть ({self._state_dir})")
            hit = self._state.hit
            if self._drag is not None:
                what = {"lift": "подъём", "move": "перемещение"}.get(self._drag_mode, "поворот")
                parts.append(f"Модель закреплена — {what} мышью" if self._pin is not None
                             else f"{what.capitalize()} мышью")
            if self._state.hover:
                hover = self._state.hover.replace("/", "\\").rsplit("\\", 1)[-1]
                parts.append(f"Под курсором: {hover}")
            if hit is not None:
                parts.append(f"Курсор: X {hit[0]:.1f}  Z {hit[2]:.1f}  высота {hit[1]:.2f}")
            else:
                parts.append("Курсор: не на земле")
            if self._state.ghost_failed and self.window.ghost.isChecked():
                parts.append("Модель у курсора не создалась (путь модели?)")
        else:
            where = ", ".join(str(c) for c in self._candidates()[:2])
            parts.append(f"Нет связи со скриптом: нажмите F10 в Buldozer. Жду файл в {where}")
        if self.camera.active:
            if self._cam_mouse:
                parts.append(f"Камера: обзор мышью ({self.camera.speed:.0f} м/с)")
            elif self._cam_keys:
                parts.append(f"Камера: {self.camera.speed:.0f} м/с — {self.camera_hint()}")
            else:
                parts.append("Камера: ждёт, пока активен Buldozer")
        elif self._cam_disabled and self._wanted:
            parts.append(f"Своя камера выключена: {self._cam_disabled}")
        if self._message:
            parts.append(self._message)
        self.window.status.setText("\n".join(parts))
