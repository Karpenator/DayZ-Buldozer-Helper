"""Правки объектов, которые уже стоят на карте (из Terrain Builder), и их запись в копии слоёв.

Объект карты выбирают в Buldozer (скрипт моста сообщает модель и где он стоит), двигают и
крутят там же. Сам Buldozer такие правки в Terrain Builder не передаёт, поэтому окно запоминает
их (:class:`MapEdit`) и по кнопке пишет **копии** слоёв ``*.tv4l`` с переставленными объектами
(оригиналы проекта не трогаем; заменить файлы — при закрытом Terrain Builder).

Как TB хранит объект (проверено по ``Evelone2666.wrp``, все 5443 объекта, см. :mod:`tbproject`):
в слое — **центр** объекта ``начало модели в Buldozer + начало рамки карты − R(курс)·delta``
(delta — сдвиг центра модели из проекта ``*.tv4p``), курс — как в Buldozer, высота над землёй —
отдельное поле (Relative). У части моделей (TB не нашёл ``.p3d``) delta при постановке не
учитывается — поэтому проверяем обе гипотезы и берём ту, что совпала с местом в слое.

Как найти объект в слое:

* шаблон — по модели: имена шаблонов с этой моделью из ``TemplateLibs`` → sdbm-хэш (так TB
  хранит шаблон у объекта); если модели нет в библиотеках — имя файла без ``.p3d``;
* место — с проектом ``*.tv4p`` точно (рамка + delta, допуск :data:`PROJECT_TOL`); без проекта —
  постоянный сдвиг, найденный по выбранным объектам (:func:`find_offset`, допуск ~0.3 м).

Что пишется: место — по формуле выше от нового места в Buldozer (поворот вокруг начала модели
сдвигает центр), курс — как повернули, высота — прежняя + подъём (Alt+ПКМ).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable

from .tbproject import TbProject, rotate_delta
from .tv4l import LayerObject, Tv4lLayer, sdbm

MATCH_TOL = 0.30          # м: без проекта — насколько место в слое может отличаться от Buldozer
PROJECT_TOL = 0.05        # м: с проектом (рамка + delta) — объект должен совпасть почти точно
YAW_TOL = 1.0             # °: курс в слое и в Buldozer
EPS = 0.0005


def norm_model(model: str) -> str:
    """Путь модели для сравнения: без регистра, через «\\», без диска и ведущих слешей."""
    p = model.replace("/", "\\").strip().lower()
    if len(p) > 2 and p[1] == ":":
        p = p[2:]
    return p.strip("\\")


@dataclass
class MapEdit:
    """Объект карты, который передвинули/повернули в Buldozer."""
    model: str
    x0: float                 # где стоял (Buldozer, начало модели)
    z0: float
    yaw0: float
    rel0: float = 0.0         # высота начала над землёй до правки
    x: float = 0.0            # где теперь
    z: float = 0.0
    yaw: float = 0.0
    lift: float = 0.0         # подъём относительно исходной высоты над землёй, м
    sess: int = 0             # сеанс скрипта и номер объекта в его реестре (для команд T)
    ref: int = -1
    saved: bool = False       # записан в копии слоёв
    deleted: bool = False     # удалён (Delete): в Buldozer спрятан, из слоя убирается

    @property
    def changed(self) -> bool:
        return (self.deleted or abs(self.x - self.x0) > EPS or abs(self.z - self.z0) > EPS
                or abs(angle_delta(self.yaw, self.yaw0)) > 0.01 or abs(self.lift) > EPS)

    def command(self) -> str:
        """``T|сеанс|номер|x|z|курс|подъём`` (+ ``|1`` — удалён: скрипт v16 прячет объект)."""
        fields = ["T", str(self.sess), str(self.ref), _num(self.x), _num(self.z),
                  _num(self.yaw % 360.0), _num(self.lift)]
        if self.deleted:
            fields.append("1")
        return "|".join(fields)

    def same_object(self, model: str, x0: float, z0: float) -> bool:
        return (norm_model(model) == norm_model(self.model)
                and abs(x0 - self.x0) < 0.01 and abs(z0 - self.z0) < 0.01)


def angle_delta(a: float, b: float) -> float:
    """a − b в градусах, в пределах (−180, 180]."""
    d = (a - b) % 360.0
    return d - 360.0 if d > 180.0 else d


def _num(v: float) -> str:
    text = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


@dataclass
class Sample:
    """Выбранный объект карты: модель и где он стоял в Buldozer (для поиска сдвига)."""
    model: str
    x: float
    z: float


def template_hashes(model: str, names_for_model: Callable[[str], Iterable[str]] | None,
                    layer_names: dict[int, str] | None = None) -> set[int]:
    """Хэши шаблонов модели. ``layer_names`` — имена шаблонов из заголовков слоёв: с ними имя
    файла сравнивается без регистра (шаблон ``Forklift`` у модели ``forklift.p3d``)."""
    names = set(names_for_model(model) if names_for_model else ())
    stem = PureWindowsPath(model.replace("/", "\\")).name
    if stem.lower().endswith(".p3d"):
        stem = stem[:-4]
    if stem:
        names.add(stem)
    # имя файла и имя шаблона в нижнем регистре: TB считает хэш по имени из импортированного
    # текста как есть, и объекты, импортированные строкой «fusebox», ссылаются на sdbm("fusebox")
    out = {sdbm(v) for n in names if n for v in (n, n.lower())}
    if layer_names:
        low = {n.lower() for n in names if n}
        out |= {h for h, n in layer_names.items() if n.lower() in low}
    return out


def _by_template(layers: Iterable[Tv4lLayer]) -> dict[int, list[tuple[Tv4lLayer, LayerObject]]]:
    out: dict[int, list[tuple[Tv4lLayer, LayerObject]]] = {}
    for layer in layers:
        for o in layer.objects:
            out.setdefault(o.template, []).append((layer, o))
    return out


def find_offset(samples: Iterable[Sample], index: dict[int, list[tuple[Tv4lLayer, LayerObject]]],
                names_for_model: Callable[[str], Iterable[str]] | None,
                tol: float = MATCH_TOL) -> tuple[tuple[float, float] | None, str]:
    """Сдвиг «слой − Buldozer» по выбранным объектам. (сдвиг или None, пояснение)."""
    per: list[list[tuple[float, float]]] = []
    seen: list[Sample] = []
    for s in samples:
        if any(abs(s.x - t.x) < 0.01 and abs(s.z - t.z) < 0.01 for t in seen):
            continue                    # один и тот же объект выбрали дважды
        seen.append(s)
        cands = [(o.x - s.x, o.z - s.z)
                 for h in template_hashes(s.model, names_for_model) for _l, o in index.get(h, ())]
        if cands:
            per.append(cands)
    if not per:
        return None, "ни один выбранный объект не найден в слоях (по шаблону)"
    grids = []
    for cands in per:
        cells: set[tuple[int, int]] = set()
        for c in cands:
            cells.add((math.floor(c[0] / tol), math.floor(c[1] / tol)))
        grids.append(cells)

    def score(c: tuple[float, float]) -> int:
        cx, cz = math.floor(c[0] / tol), math.floor(c[1] / tol)
        return sum(1 for cells in grids
                   if any((cx + i, cz + j) in cells for i in (-1, 0, 1) for j in (-1, 0, 1)))

    scored = [(score(c), c) for cands in per for c in cands]
    top = max(s for s, _c in scored)
    clusters: list[tuple[float, float]] = []
    for s_, c in scored:
        if s_ == top and not any(abs(c[0] - k[0]) <= 2 * tol and abs(c[1] - k[1]) <= 2 * tol
                                 for k in clusters):
            clusters.append(c)
    if len(clusters) > 1 or (top == 1 and len(per) > 1):
        return None, ("сдвиг координат слоя пока не ясен — выделите ещё 1–2 разных объекта карты "
                      "(лучше редких моделей)")
    best, best_score = clusters[0], top
    # уточнить: среднее по всем совпавшим
    xs, zs = [], []
    for cands in per:
        near = [d for d in cands if abs(best[0] - d[0]) <= tol and abs(best[1] - d[1]) <= tol]
        if near:
            d = min(near, key=lambda d: math.hypot(d[0] - best[0], d[1] - best[1]))
            xs.append(d[0])
            zs.append(d[1])
    return (sum(xs) / len(xs), sum(zs) / len(zs)), f"сдвиг найден по {best_score} объект(ам)"


@dataclass
class SaveReport:
    written: list[Path] = field(default_factory=list)
    moved: int = 0
    not_found: list[MapEdit] = field(default_factory=list)
    lift_skipped: int = 0
    lifted: int = 0
    deleted: int = 0                  # убрано из слоёв (удалённые объекты карты)
    offset: tuple[float, float] | None = None
    project: Path | None = None
    no_delta: int = 0                 # объекты, которые TB ставит без сдвига центра модели
    message: str = ""


def _candidates(index, e: MapEdit, names_for_model,
                layer_names: dict[int, str] | None = None) -> list[tuple[Tv4lLayer, LayerObject]]:
    return [lo for h in template_hashes(e.model, names_for_model, layer_names)
            for lo in index.get(h, ())]


def _match_project(e: MapEdit, cands, project: TbProject, used: set[int], tol: float):
    """Объект слоя для правки по рамке и delta проекта: (расстояние, слой, объект, delta) или None."""
    delta = project.delta(e.model) or (0.0, 0.0)
    best = None
    for d in ((delta, (0.0, 0.0)) if any(delta) else ((0.0, 0.0),)):
        rx, rz = rotate_delta(d[0], d[1], e.yaw0)
        want = (e.x0 + project.origin[0] - rx, e.z0 + project.origin[1] - rz)
        for layer, o in cands:
            if id(o) in used or abs(angle_delta(o.yaw, e.yaw0)) > YAW_TOL:
                continue
            dist = math.hypot(o.x - want[0], o.z - want[1])
            if dist <= tol and (best is None or dist < best[0]):
                best = (dist, layer, o, d)
    return best


def apply_edits(layers: list[Tv4lLayer], edits: Iterable[MapEdit],
                names_for_model: Callable[[str], Iterable[str]] | None = None,
                offset: tuple[float, float] | None = None,
                samples: Iterable[Sample] = (), tol: float = MATCH_TOL,
                project: TbProject | None = None) -> tuple[SaveReport, dict[int, Tv4lLayer]]:
    """Переставить объекты карты в загруженных слоях (в памяти). (отчёт, изменённые слои)."""
    rep = SaveReport()
    touched: dict[int, Tv4lLayer] = {}
    todo = [e for e in edits if e.changed]
    if not todo:
        rep.message = "нет изменённых объектов карты"
        return rep, touched
    index = _by_template(layers)
    layer_names: dict[int, str] = {}
    for layer in layers:
        layer_names.update(getattr(layer, "template_names", {}) or {})
    if project is not None:
        rep.project = project.path
        rep.offset = project.origin
    else:
        if offset is None:
            offset, why = find_offset([Sample(e.model, e.x0, e.z0) for e in todo] + list(samples),
                                      index, names_for_model, tol)
            if offset is None:
                rep.message = why
                rep.not_found = list(todo)
                return rep, touched
        rep.offset = offset
    used: set[int] = set()
    for e in todo:
        cands = _candidates(index, e, names_for_model, layer_names)
        delta = (0.0, 0.0)
        if project is not None:
            best = _match_project(e, cands, project, used, PROJECT_TOL)
            if best is not None:
                delta = best[3]
                if not any(delta) and any(project.delta(e.model) or (0.0, 0.0)):
                    rep.no_delta += 1
        else:
            want = (e.x0 + offset[0], e.z0 + offset[1])
            best = None
            for layer, o in cands:
                d = math.hypot(o.x - want[0], o.z - want[1])
                if d <= tol and id(o) not in used and (best is None or d < best[0]):
                    best = (d, layer, o, delta)
        if best is None:
            rep.not_found.append(e)
            continue
        _d, layer, o = best[:3]
        used.add(id(o))
        if e.deleted:
            layer.remove(o)
            touched[id(layer)] = layer
            rep.deleted += 1
            continue
        # центр сдвигается вместе с началом модели и поворачивается вокруг него
        r0 = rotate_delta(delta[0], delta[1], e.yaw0)
        r1 = rotate_delta(delta[0], delta[1], e.yaw)
        x = o.x + (e.x - e.x0) - r1[0] + r0[0]
        z = o.z + (e.z - e.z0) - r1[1] + r0[1]
        elevation = o.elevation + e.lift if abs(e.lift) > EPS else None
        layer.move(o, x, z, o.yaw + angle_delta(e.yaw, e.yaw0), elevation)
        touched[id(layer)] = layer
        rep.moved += 1
        if elevation is not None:
            rep.lifted += 1
    rep.message = f"переставлено в слоях: {rep.moved}"
    if rep.deleted:
        rep.message += f", удалено: {rep.deleted}"
    if rep.not_found and not rep.moved and not rep.deleted:
        rep.message = ("объекты не найдены в слоях" + (" — проект и слои от одной карты? Слои должны "
                                                        "быть сохранены в TB до запуска Buldozer"
                                                        if project is not None else ""))
    return rep, touched


def save_edits(layers: list[Tv4lLayer], edits: Iterable[MapEdit], out_dir: str | Path,
               names_for_model: Callable[[str], Iterable[str]] | None = None,
               offset: tuple[float, float] | None = None,
               samples: Iterable[Sample] = (), tol: float = MATCH_TOL,
               project: TbProject | None = None) -> SaveReport:
    """Записать копии слоёв с переставленными объектами в ``out_dir`` (только изменённые слои)."""
    edits = list(edits)
    rep, touched = apply_edits(layers, edits, names_for_model, offset, samples, tol, project)
    if touched:
        out = Path(out_dir)
        for layer in touched.values():
            rep.written.append(layer.save(out / f"{layer.name}.tv4l"))
        for e in edits:
            if e.changed and e not in rep.not_found:
                e.saved = True
    return rep


def edited_dir(layers_dir: Path, now: float | None = None) -> Path:
    """Куда класть копии: рядом со слоями, своя папка на каждое сохранение."""
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(now))
    return layers_dir.parent / "WE_edited_layers" / stamp
