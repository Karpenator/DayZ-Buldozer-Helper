"""Рабочая копия слоёв Terrain Builder: свои объекты + правки объектов карты (без Qt).

Каждый раз собирается заново из **оригинальных** слоёв проекта (``<Проект>.Layers\\*.tv4l``):
в выбранный слой добавляются поставленные объекты (F9), объекты карты переставляются по
правкам (:mod:`map_edits`). Результат пишется в ``<папка TB>\\WE_edited_layers\\current`` —
поэтому повторная запись ничего не удваивает, а «Отменить» убирает объект и из копии.

Почему не сразу в файлы проекта: Terrain Builder держит слои в памяти — пока он открыт (а
Buldozer запускается из него), новых файлов он не увидит и при сохранении проекта перезапишет
их своими. Поэтому «Применить в проект» (:func:`apply_to_project`) — только при закрытом TB и
с резервной копией заменяемых файлов.

Место нового объекта в слое (проверено на всех 70 моделях проекта Evelone2666): TB хранит
исходное начало модели (MLOD) — ``начало в Buldozer + рамка карты − масштаб·R(курс)·delta``,
где delta модели — центр её габаритов из ``TemplateLibs\\*.tml`` по X/Z (совпадает с
``mobjectsDelta`` проекта до 1 мкм). Высота (Relative) — посчитанная скриптом в Buldozer
(``tb_rel``), иначе ``подъём − низ габаритов·масштаб``.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import debuglog
from .map_edits import MapEdit, Sample, apply_edits
from .tbproject import TbProject, norm_model, rotate_delta
from .tv4l import Tv4lError, Tv4lLayer, diagnose, sdbm

log = debuglog.get_logger("layers")
_diagnosed: set[tuple] = set()      # (файл, размер, время) — подробности в лог по разу на версию

WORK_DIR_NAME = "current"
ORIGINALS_DIR = ".originals"     # копии оригиналов слоёв: пока TB держит файлы, читаем их
EDITED_ROOT = "WE_edited_layers"
BACKUP_ROOT = "WE_layers_backup"
TB_EXE = "terrainbuilder.exe"


@dataclass
class SyncReport:
    written: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    added: int = 0
    moved: int = 0
    lifted: int = 0
    deleted: int = 0               # удалённые объекты карты, убранные из слоёв
    target: str = ""
    skipped: list[tuple[str, str]] = field(default_factory=list)    # (модель, почему)
    not_found: list[MapEdit] = field(default_factory=list)
    edits_message: str = ""
    load_errors: list[str] = field(default_factory=list)   # слои, которые не прочитались
    from_cache: list[str] = field(default_factory=list)    # слои из копии (файл занят TB)
    edits_project: Path | None = None
    offset: tuple[float, float] | None = None
    already: int = 0               # объекты сеанса, которые уже есть в слоях (TB их сохранил)
    frame_warning: str = ""        # рамка карты проекта не сходится с объектами слоёв
    error: str = ""

    @property
    def message(self) -> str:
        if self.error:
            return self.error
        parts = []
        if self.added:
            parts.append(f"новых в «{self.target}»: {self.added}")
        if self.moved:
            parts.append(f"передвинуто: {self.moved}")
        if self.deleted:
            parts.append(f"удалено: {self.deleted}")
        if self.skipped:
            parts.append(f"пропущено: {len(self.skipped)}")
        if self.not_found:
            parts.append(f"не найдено в слоях: {len(self.not_found)}")
        if self.already:
            parts.append(f"уже в проекте: {self.already}")
        return ", ".join(parts) if parts else "изменений нет"


def work_dir(layers_dir: Path) -> Path:
    return Path(layers_dir).parent / EDITED_ROOT / WORK_DIR_NAME


def originals_dir(layers_dir: Path) -> Path:
    return Path(layers_dir).parent / EDITED_ROOT / ORIGINALS_DIR


def _load_meta(folder: Path) -> dict:
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def read_layers(layers_dir: Path, cache: Path | None = None
                ) -> tuple[list[Tv4lLayer], list[str], list[str]]:
    """Прочитать оригиналы слоёв. (слои, ошибки по файлам, какие взяты из копии).

    Пока открыт Terrain Builder, он может держать файлы слоёв занятыми. Поэтому каждый
    прочитанный файл копируется в ``WE_edited_layers\\.originals`` (с размером и временем
    изменения), а если файл не открывается, но не менялся с тех пор — берётся копия.
    """
    layers_dir = Path(layers_dir)
    cache = Path(cache) if cache is not None else originals_dir(layers_dir)
    meta = _load_meta(cache)
    meta_changed = False
    layers, errors, cached = [], [], []
    for p in sorted(layers_dir.glob("*.tv4l"), key=lambda q: q.name.lower()):
        key = p.name.lower()
        try:
            st = p.stat()
        except OSError as exc:
            errors.append(f"{p.name}: {exc.strerror or exc}")
            continue
        stamp = [st.st_size, st.st_mtime_ns]
        try:
            data = p.read_bytes()
        except OSError as exc:
            copy = cache / p.name
            if meta.get(key) == stamp and copy.is_file():
                try:
                    data = copy.read_bytes()
                except OSError:
                    data = None
                if data is not None:
                    cached.append(p.name)
            else:
                data = None
            if data is None:
                errors.append(f"{p.name}: файл не открывается ({exc.strerror or exc}) — занят "
                              "Terrain Builder? Копии нет: закройте TB один раз, чтобы программа "
                              "её сделала")
                continue
        else:
            if meta.get(key) != stamp:
                try:
                    cache.mkdir(parents=True, exist_ok=True)
                    tmp = cache / (p.name + ".tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, cache / p.name)
                    meta[key] = stamp
                    meta_changed = True
                except OSError:
                    pass
        try:
            layers.append(Tv4lLayer(data, p))
        except (Tv4lError, struct.error, IndexError, ValueError) as exc:
            errors.append(f"{p.name}: не разобран ({exc})")
            sig = (str(p).lower(), len(data), stamp[1])
            if sig not in _diagnosed:
                _diagnosed.add(sig)
                try:
                    details = diagnose(data)
                except Exception as dexc:          # noqa: BLE001 - диагностика не должна мешать
                    details = [f"диагностика не удалась: {dexc!r}"]
                log.warning("layer %s not parsed (%s):\n%s", p, exc, "\n".join(details))
    if meta_changed:
        try:
            (cache / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        except OSError:
            pass
    return layers, errors, cached


def template_delta(template) -> tuple[float, float] | None:
    """delta модели по шаблону: центр габаритов из .tml по X/Z (None — габаритов нет)."""
    lo, hi = getattr(template, "bbox_min", None), getattr(template, "bbox_max", None)
    if not lo or not hi:
        return None
    return (lo[0] + hi[0]) / 2.0, (lo[2] + hi[2]) / 2.0


def model_delta(model: str, project: TbProject | None, template) -> tuple[float, float]:
    if project is not None:
        d = project.delta(model)
        if d is not None:
            return d
    return template_delta(template) or (0.0, 0.0)


def placed_elevation(obj, template) -> float:
    """Relative для TB: от скрипта (точно, с землёй под исходным началом) или по габаритам."""
    rel = getattr(obj, "tb_rel", None)
    if rel is not None:
        return float(rel)
    lo = getattr(template, "bbox_min", None)
    scale = float(getattr(obj, "scale", 1.0) or 1.0)
    return float(obj.lift) - (lo[1] * scale if lo else 0.0)


def pick_layer(layers: list[Tv4lLayer], name: str) -> Tv4lLayer | None:
    want = (name or "").strip().lower()
    for layer in layers:
        if layer.name.lower() == want:
            return layer
    return None


SAME_POS_M = 0.05                  # объект слоя «тот же», если центр ближе 5 см,
SAME_YAW_DEG = 1.0                 # курс отличается меньше чем на 1° и шаблон тот же


class _Present:
    """Объекты, которые уже есть в слоях: поиск «такого же» по шаблону и месту (сетка 1 м)."""

    def __init__(self, layers: list[Tv4lLayer]) -> None:
        self.grid: dict[tuple[int, int], list] = {}
        for layer in layers:
            for o in layer.objects:
                self.grid.setdefault((math.floor(o.x), math.floor(o.z)), []).append(o)

    def has(self, template: int, x: float, z: float, yaw: float) -> bool:
        gx, gz = math.floor(x), math.floor(z)
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for o in self.grid.get((gx + i, gz + j), ()):
                    if (o.template == template and math.hypot(o.x - x, o.z - z) < SAME_POS_M
                            and abs((o.yaw - yaw + 180.0) % 360.0 - 180.0) < SAME_YAW_DEG):
                        return True
        return False


def frame_check(layers: list[Tv4lLayer], project: TbProject | None) -> str:
    """Предупреждение, если почти все объекты слоёв лежат вне рамки карты проекта (рамку
    передвинули, а объекты — нет: в Buldozer они окажутся вне карты, а новые встанут иначе)."""
    if project is None:
        return ""
    x0, z0, x1, z1 = project.frame
    total = inside = 0
    for layer in layers:
        for o in layer.objects:
            total += 1
            inside += x0 <= o.x <= x1 and z0 <= o.z <= z1
    if total < 10 or inside * 2 >= total:
        return ""
    xs = sorted(o.x for layer in layers for o in layer.objects)
    zs = sorted(o.z for layer in layers for o in layer.objects)
    mid = len(xs) // 2
    return (f"рамка карты в проекте {project.path.name if project.path else ''} начинается в "
            f"({x0:.0f}, {z0:.0f}), а объекты слоёв — около ({xs[mid]:.0f}, {zs[mid]:.0f}): внутри "
            f"рамки {inside} из {total}. Проверьте Mapframe в Terrain Builder (Easting/Northing)")


def build(layers: list[Tv4lLayer], objects: Iterable, edits: Iterable[MapEdit], target: str,
          template_for: Callable[[str], object | None], project: TbProject | None,
          names_for_model: Callable[[str], Iterable[str]] | None = None,
          samples: Iterable[Sample] = (),
          offset: tuple[float, float] | None = None) -> tuple[SyncReport, dict[str, Tv4lLayer]]:
    """Изменить загруженные слои: добавить свои объекты в ``target``, применить правки карты.

    ``template_for(model)`` — шаблон TB модели (имя, библиотека, габариты) или None.
    Возвращает отчёт и изменённые слои по имени.
    """
    rep = SyncReport(target=target)
    touched: dict[str, Tv4lLayer] = {}
    objects = [o for o in objects if not getattr(o, "failed", False)]
    rep.frame_warning = frame_check(layers, project) if objects or edits else ""
    edits = [e for e in edits if e.changed]
    if edits:
        er, moved = apply_edits(layers, edits, names_for_model, offset, samples, project=project)
        rep.moved, rep.lifted, rep.not_found, rep.edits_message = er.moved, er.lifted, er.not_found, er.message
        rep.deleted = er.deleted
        rep.edits_project, rep.offset = er.project, er.offset
        for layer in moved.values():
            touched[layer.name] = layer
    if objects:
        layer = pick_layer(layers, target)
        if layer is None:
            rep.error = f"нет слоя «{target}» — выберите слой для новых объектов"
            rep.skipped += [(o.model, "нет слоя") for o in objects]
            return rep, touched
        if project is None:
            rep.error = "не найден проект *.tv4p — без рамки карты новые объекты не записать"
            rep.skipped += [(o.model, "нет проекта") for o in objects]
            return rep, touched
        present = _Present(layers)
        for o in objects:
            tpl = template_for(o.model)
            if tpl is None:
                rep.skipped.append((o.model, "модели нет в библиотеках шаблонов TB"))
                continue
            scale = float(o.scale or 1.0)
            dx, dz = model_delta(o.model, project, tpl)
            rx, rz = rotate_delta(dx * scale, dz * scale, o.yaw)
            lx = o.x + project.origin[0] - rx
            lz = o.z + project.origin[1] - rz
            if present.has(sdbm(tpl.name), lx, lz, o.yaw):
                rep.already += 1           # TB уже сохранил его (вставка в TB) — не удваивать
                continue
            try:
                layer.add_object(tpl.name, tpl.library, lx, lz, o.yaw, scale, placed_elevation(o, tpl))
            except Tv4lError as exc:
                rep.skipped.append((o.model, str(exc)))
                continue
            rep.added += 1
            touched[layer.name] = layer
    return rep, touched


def write_dir(touched: dict[str, Tv4lLayer], out_dir: Path, rep: SyncReport,
              keep: Iterable[str] = ()) -> None:
    """Записать изменённые слои; устаревшие копии (слой больше не меняется) — убрать
    (кроме ``keep`` — слоёв, которые сейчас не прочитались)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, layer in sorted(touched.items()):
        rep.written.append(layer.save(out_dir / f"{name}.tv4l"))
    keep = {p.name.lower() for p in rep.written} | {k.lower() for k in keep}
    for p in out_dir.glob("*.tv4l"):
        if p.name.lower() not in keep:
            try:
                p.unlink()
                rep.removed.append(p)
            except OSError:
                pass


def sync(layers_dir: Path, objects: Iterable, edits: Iterable[MapEdit], target: str,
         template_for: Callable[[str], object | None], project: TbProject | None,
         names_for_model: Callable[[str], Iterable[str]] | None = None,
         samples: Iterable[Sample] = (), out_dir: Path | None = None,
         offset: tuple[float, float] | None = None) -> SyncReport:
    """Собрать рабочую копию заново из оригиналов слоёв и записать её (по умолчанию в current)."""
    layers_dir = Path(layers_dir)
    objects = list(objects)
    out = Path(out_dir) if out_dir is not None else work_dir(layers_dir)
    layers, errors, cached = read_layers(layers_dir)
    if not layers:
        why = "; ".join(errors) if errors else f"в папке нет слоёв *.tv4l: {layers_dir}"
        return SyncReport(target=target, error=f"слои не прочитаны: {why}", load_errors=errors)
    rep, touched = build(layers, objects, edits, target, template_for, project, names_for_model,
                         samples, offset)
    rep.load_errors, rep.from_cache = errors, cached
    if errors:
        bad = {e.split(":", 1)[0].lower() for e in errors}
        if f"{target.lower()}.tv4l" in bad and objects:
            rep.error = "слой не прочитан: " + next(e for e in errors
                                                     if e.lower().startswith(target.lower() + ".tv4l"))
    if rep.error and errors:
        # не трогать рабочую копию, раз часть слоёв не прочиталась — в ней могли быть их правки
        return rep
    write_dir(touched, out, rep, keep={e.split(":", 1)[0] for e in errors})
    return rep


def tb_running() -> bool:
    """Запущен ли Terrain Builder (только Windows; иначе False)."""
    if os.name != "nt":
        return False
    try:                                                    # pragma: no cover - нужен Windows
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {TB_EXE}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return TB_EXE in out.lower()                            # pragma: no cover - нужен Windows


def apply_to_project(work: Path, layers_dir: Path, now: float | None = None) -> tuple[list[Path], Path]:
    """Скопировать слои рабочей копии в папку слоёв проекта; заменяемые файлы — в резервную
    папку ``<папка TB>\\WE_layers_backup\\<дата-время>``. (заменённые файлы, папка копий)."""
    work, layers_dir = Path(work), Path(layers_dir)
    files = sorted(work.glob("*.tv4l"))
    if not files:
        raise FileNotFoundError(f"в рабочей копии нет слоёв: {work}")
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(now))
    backup = layers_dir.parent / BACKUP_ROOT / stamp
    backup.mkdir(parents=True, exist_ok=True)
    for f in files:
        Tv4lLayer.load(f)                                   # битый файл в проект не пустим
    done = []
    for f in files:
        dest = layers_dir / f.name
        if dest.exists():
            shutil.copy2(dest, backup / f.name)
        tmp = dest.with_name(dest.name + ".we_tmp")
        shutil.copy2(f, tmp)
        os.replace(tmp, dest)
        done.append(dest)
    return done, backup


def layer_names(layers_dir: Path | None) -> list[str]:
    if layers_dir is None or not Path(layers_dir).is_dir():
        return []
    return sorted((p.stem for p in Path(layers_dir).glob("*.tv4l")), key=str.lower)


def same_model(a: str, b: str) -> bool:
    return norm_model(a) == norm_model(b)
