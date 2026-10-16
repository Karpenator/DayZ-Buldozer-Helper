"""Слои объектов Terrain Builder (``<Проект>.Layers\\*.tv4l``) — чтение и запись копий (без Qt).

Формат разобран по файлам карты Evelone2666 (TB для DayZ). Файл — двоичный «ALB1»:

* заголовок ``ALB1`` + 12 байт;
* таблица имён полей (секция 2) и таблица имён классов (секция 3):
  ``u16 секция, u8 тип, u32 количество`` и записи ``u16 id, u16 длина, текст``;
* данные — поля ``u16 id поля, u8 тип, значение``. Объекты лежат в одном поле ``tree``
  (тип 0x0c — блок: ``u32 размер`` и внутри ещё ``u32 размер``) — квадродерево:

  - узел: ``u8 вид, 4×f64 границы (x0, z0, x1, z1), u32 глубина, f32, u8 маска потомков``;
    вид 0x01 — потомки узлы, 0x10 — потомки листья (глубина 13), 0xFF — пустой узел;
    биты маски: 0 — (x меньше, z больше), 1 — (x больше, z больше), 2 — (x меньше, z меньше),
    3 — (x больше, z меньше); границы потомка — ровно половина границ родителя;
  - лист: ``4×f64 границы, f32, u32, u32 число групп`` и группы
    ``u32 число объектов, u32 хэш шаблона``, затем объекты по 40 байт:
    ``f64 x, f64 z, f32 курс (°), f32 тангаж, f32 крен, f32 масштаб, f32 высота, u32 id``;
    в слоях ``mlayerVersion`` 5 (DayZ Tools 1.30) — по 44 байта: после id ещё 4 байта
    (у всех объектов нули; храним как есть, новым объектам пишем нули).

  Поле f32 у узлов/листьев и u32 у листа TB, судя по всему, не заполняет (там мусор) —
  храним их байты как есть.

* хэш шаблона = sdbm(имя шаблона), как в ``TemplateLibs\\*.tml`` (``EV_1`` → 0xB50E9063);
  имена шаблонов слоя перечислены в его заголовке (второе поле ``libs``: имя → хэш библиотеки).

Проверено по ``Evelone2666.wrp`` (5443 объекта): x/z — **центр** объекта в координатах TB
(``начало модели в Buldozer + начало рамки карты − R(курс)·delta модели``, см. :mod:`tbproject`);
курс — тот же, что у объекта в Buldozer; «высота» — поле Relative в TB (высота над землёй,
м): Y в Buldozer = земля + свой сдвиг модели + высота; масштаб в .wrp не попадает.
Чтение и запись без правок дают байт-в-байт тот же файл (проверено на 4 слоях).

Новый объект (:meth:`Tv4lLayer.add_object`): шаблон записывается в заголовок (второй список
``libs``, по алфавиту; библиотека — в первый), id = ``mobjectIDcounter`` (следующий свободный,
затем +1), ``objectCount`` пересчитывается. Новые узлы/листья — с нулями в «пустых» полях,
как у большинства узлов TB.

Оригиналы слоёв не трогаем: :func:`Tv4lLayer.save` отказывается писать поверх файла,
из которого слой прочитан.
"""
from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"ALB1"
OBJECT_SIZE = 40                 # mlayerVersion 4
OBJECT_TAIL_V5 = 4               # mlayerVersion 5+: 4 байта после id
KIND_NODES = 0x01
KIND_LEAVES = 0x10
KIND_EMPTY = 0xFF
TYPE_BLOCK = 0x0C
NEG_INF = struct.pack("<f", float("-inf"))
ZERO4 = b"\0\0\0\0"


class Tv4lError(ValueError):
    """Ошибка слоя. При сбое разбора дерева: ``pos`` — смещение в файле, ``trail`` — последние
    прочитанные узлы/листья (для :func:`diagnose`)."""

    pos: int | None = None
    trail: tuple = ()


def sdbm(text: str) -> int:
    """Хэш имени шаблона, как его хранит TB (sdbm по байтам имени)."""
    h = 0
    for c in text.encode("utf-8"):
        h = (c + (h << 6) + (h << 16) - h) & 0xFFFFFFFF
    return h


@dataclass
class LayerObject:
    id: int
    template: int                   # sdbm(имя шаблона)
    x: float
    z: float
    floats: bytes                   # 5×f32 как в файле: курс, тангаж, крен, масштаб, высота
    layer: str = ""
    tail: bytes = b""               # слой v5: 4 байта после id (как в файле)

    @property
    def yaw(self) -> float:
        return struct.unpack_from("<f", self.floats, 0)[0]

    @property
    def values(self) -> tuple[float, float, float, float, float]:
        return struct.unpack("<5f", self.floats)

    @property
    def scale(self) -> float:
        return struct.unpack_from("<f", self.floats, 12)[0]

    @property
    def elevation(self) -> float:
        """Высота над землёй (поле Relative в TB), м."""
        return struct.unpack_from("<f", self.floats, 16)[0]

    def set_yaw(self, yaw: float) -> None:
        self.floats = struct.pack("<f", float(yaw) % 360.0) + self.floats[4:]

    def set_elevation(self, value: float) -> None:
        self.floats = self.floats[:16] + struct.pack("<f", float(value))


@dataclass
class Group:
    template: int
    objects: list[LayerObject] = field(default_factory=list)


@dataclass
class Leaf:
    bbox: tuple[float, float, float, float]
    y: bytes = b"\0\0\0\0"
    a: bytes = b"\0\0\0\0"
    groups: list[Group] = field(default_factory=list)


@dataclass
class Node:
    kind: int
    bbox: tuple[float, float, float, float]
    depth: int
    y: bytes = NEG_INF
    children: dict[int, "Node | Leaf"] = field(default_factory=dict)

    @property
    def mask(self) -> int:
        m = 0
        for bit in self.children:
            m |= 1 << bit
        return m


def child_bbox(bbox: tuple[float, float, float, float], bit: int) -> tuple[float, float, float, float]:
    x0, z0, x1, z1 = bbox
    mx, mz = (x0 + x1) / 2.0, (z0 + z1) / 2.0
    right = bit in (1, 3)
    top = bit in (0, 1)
    return (mx if right else x0, mz if top else z0, x1 if right else mx, z1 if top else mz)


def child_bit(bbox: tuple[float, float, float, float], x: float, z: float) -> int:
    x0, z0, x1, z1 = bbox
    mx, mz = (x0 + x1) / 2.0, (z0 + z1) / 2.0
    return (0 if z >= mz else 2) + (1 if x >= mx else 0)


class _Reader:
    def __init__(self, data: bytes, pos: int) -> None:
        self.d = data
        self.p = pos
        self.trail: deque = deque(maxlen=12)   # (смещение, что, вид/групп, глубина, маска)

    def take(self, n: int) -> bytes:
        if self.p + n > len(self.d):
            raise Tv4lError("файл слоя обрывается")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def bbox(self) -> tuple[float, float, float, float]:
        return struct.unpack("<4d", self.take(32))


def _name_table(data: bytes, pos: int) -> tuple[dict[int, str], int]:
    _sec, _typ, count = struct.unpack_from("<HBI", data, pos)
    pos += 7
    names: dict[int, str] = {}
    for _ in range(count):
        i, n = struct.unpack_from("<HH", data, pos)
        names[i] = data[pos + 4:pos + 4 + n].decode("latin-1")
        pos += 4 + n
    return names, pos


class Tv4lLayer:
    """Один слой TB: все объекты + дерево (для записи изменённой копии)."""

    def __init__(self, data: bytes, path: Path | None = None) -> None:
        self.path = path
        self.name = path.stem if path else ""
        self._data = data
        if data[:4] != MAGIC:
            raise Tv4lError("не слой Terrain Builder (нет ALB1)")
        fields, pos = _name_table(data, 16)
        _classes, pos = _name_table(data, pos)
        ids = {v: k for k, v in fields.items()}
        if "tree" not in ids:
            raise Tv4lError("в слое нет поля tree")
        tag = struct.pack("<HB", ids["tree"], TYPE_BLOCK)
        at = data.find(tag, pos)
        while at >= 0:
            size, inner = struct.unpack_from("<II", data, at + 3)
            if inner + 4 == size and at + 7 + size <= len(data):
                break
            at = data.find(tag, at + 1)
        if at < 0:
            raise Tv4lError("не найдено дерево объектов")
        self.version = self._layer_version(data, ids, pos, at)
        self._tail = OBJECT_TAIL_V5 if self.version >= 5 else 0
        self._tree_at = at
        self._tree_end = at + 7 + size
        r = _Reader(data, at + 11)
        try:
            self.root = self._read_node(r)
            if r.p != self._tree_end:
                raise Tv4lError(f"дерево разобрано не до конца ({r.p} из {self._tree_end})")
        except Tv4lError as exc:
            exc.pos, exc.trail = r.p, tuple(r.trail)
            raise
        self.leaf_depth = self._find_leaf_depth(self.root)
        self.template_names = self._read_template_names(data)
        self._read_header(data)
        if self.to_bytes() != data:
            # дерево разобралось, но собирается не в те же байты: такой вид слоя не знаем —
            # лучше не писать, чем испортить слой
            raise Tv4lError("слой разобран, но не собирается обратно в тот же файл "
                            "(незнакомый вариант формата)")

    @staticmethod
    def _layer_version(data: bytes, ids: dict[str, int], start: int, end: int) -> int:
        """mlayerVersion из заголовка (до дерева); не нашли — 4."""
        if "mlayerVersion" not in ids:
            return 4
        for t, fmt in ((0x05, "<i"), (0x06, "<i"), (0x07, "<I")):
            at = data.find(struct.pack("<HB", ids["mlayerVersion"], t), start, end)
            if at >= 0 and at + 7 <= end:
                return int(struct.unpack_from(fmt, data, at + 3)[0])
        return 4

    def _read_header(self, data: bytes) -> None:
        """Поля заголовка, которые меняются при добавлении объектов (если заголовок разобрался)."""
        from .tbproject import AlbError, AlbReader
        self._spans: list[tuple[str, int, int, int, int]] = []
        self._lists: list[list[tuple[str, int]]] = []
        self._field_ids: dict[str, int] = {}
        self.id_counter: int | None = None
        self._header_dirty = False
        try:
            reader = AlbReader(data)
            spans = reader.top_spans()
        except (AlbError, struct.error, IndexError, UnicodeDecodeError):
            return
        self._field_ids = {v: k for k, v in reader.fields.items()}
        lists = []
        for name, t, _start, vstart, vend in spans:
            if name == "libs" and t == TYPE_BLOCK:
                value, _ = reader._value(name, t, vstart)
                if not isinstance(value, list):
                    return
                lists.append([(str(k), int(v)) for k, v in value if isinstance(k, str)])
            elif name == "mobjectIDcounter" and t in (0x05, 0x06, 0x07):
                self.id_counter = struct.unpack_from("<i" if t != 0x07 else "<I", data, vstart)[0]
        self._spans = spans
        self._lists = lists

    @property
    def can_add(self) -> bool:
        """Можно ли добавлять объекты (заголовок разобран, есть списки библиотек и шаблонов)."""
        return (len(self._lists) == 2 and self.id_counter is not None
                and all(k in self._field_ids for k in ("pair", "key", "data", "libs")))

    @property
    def libraries(self) -> list[str]:
        return [n for n, _h in self._lists[0]] if self._lists else []

    @staticmethod
    def _read_template_names(data: bytes) -> dict[int, str]:
        """sdbm-хэш -> имя шаблона (из заголовка слоя). Пусто, если заголовок не разобрался."""
        from .tbproject import AlbError, AlbReader
        try:
            head = AlbReader(data).root(stop="tree")
        except (AlbError, struct.error, IndexError, UnicodeDecodeError):
            return {}
        names: dict[int, str] = {}
        for lst in head.all("libs"):
            for item in lst if isinstance(lst, list) else ():
                if isinstance(item, tuple) and isinstance(item[0], str):
                    names[sdbm(item[0])] = item[0]
        return names

    # ---- чтение
    def _read_node(self, r: _Reader) -> Node:
        at = r.p
        kind = r.u8()
        bbox = r.bbox()
        depth = r.u32()
        y = r.take(4)
        mask = r.u8()
        r.trail.append((at, "узел", kind, depth, mask))
        node = Node(kind, bbox, depth, y)
        for bit in range(4):
            if mask >> bit & 1:
                if kind == KIND_LEAVES:
                    node.children[bit] = self._read_leaf(r)
                elif kind == KIND_NODES:
                    node.children[bit] = self._read_node(r)
                else:
                    raise Tv4lError(f"неизвестный вид узла 0x{kind:02x}")
        return node

    def _read_leaf(self, r: _Reader) -> Leaf:
        at = r.p
        leaf = Leaf(r.bbox(), r.take(4), r.take(4))
        n_groups = r.u32()
        r.trail.append((at, "лист", n_groups, 0, 0))
        for _ in range(n_groups):
            count, tpl = r.u32(), r.u32()
            group = Group(tpl)
            for _ in range(count):
                x, z = struct.unpack("<2d", r.take(16))
                floats = r.take(20)
                oid = r.u32()
                tail = r.take(self._tail) if self._tail else b""
                group.objects.append(LayerObject(oid, tpl, x, z, floats, self.name, tail))
            leaf.groups.append(group)
        return leaf

    @staticmethod
    def _find_leaf_depth(root: Node) -> int:
        stack = [root]
        while stack:
            n = stack.pop()
            if n.kind == KIND_LEAVES:
                return n.depth + 1
            stack.extend(c for c in n.children.values() if isinstance(c, Node))
        return 14

    @classmethod
    def load(cls, path: str | Path) -> "Tv4lLayer":
        p = Path(path)
        return cls(p.read_bytes(), p)

    # ---- объекты
    def _leaves(self):
        stack = [self.root]
        while stack:
            n = stack.pop()
            for c in n.children.values():
                if isinstance(c, Leaf):
                    yield c
                else:
                    stack.append(c)

    @property
    def objects(self) -> list[LayerObject]:
        return [o for leaf in self._leaves() for g in leaf.groups for o in g.objects]

    def find(self, object_id: int) -> LayerObject | None:
        for o in self.objects:
            if o.id == object_id:
                return o
        return None

    def _leaf_of(self, obj: LayerObject) -> tuple[Leaf, Group] | None:
        for leaf in self._leaves():
            for g in leaf.groups:
                if any(o is obj for o in g.objects):
                    return leaf, g
        return None

    def _leaf_for(self, x: float, z: float) -> Leaf:
        x0, z0, x1, z1 = self.root.bbox
        if not (x0 <= x < x1 and z0 <= z < z1):
            raise Tv4lError("точка вне границ слоя")
        node = self.root
        while True:
            bit = child_bit(node.bbox, x, z)
            if node.depth + 1 >= self.leaf_depth:
                node.kind = KIND_LEAVES
                leaf = node.children.get(bit)
                if not isinstance(leaf, Leaf):
                    leaf = Leaf(child_bbox(node.bbox, bit), ZERO4, ZERO4)
                    node.children[bit] = leaf
                return leaf
            if node.kind == KIND_EMPTY:
                node.kind = KIND_NODES
            child = node.children.get(bit)
            if not isinstance(child, Node):
                kind = KIND_LEAVES if node.depth + 2 >= self.leaf_depth else KIND_NODES
                child = Node(kind, child_bbox(node.bbox, bit), node.depth + 1, ZERO4)
                node.children[bit] = child
            node = child

    def add_object(self, template: str, library: str, x: float, z: float, yaw: float = 0.0,
                   scale: float = 1.0, elevation: float = 0.0, pitch: float = 0.0,
                   roll: float = 0.0) -> LayerObject:
        """Новый объект слоя (x/z — координаты слоя TB, то есть исходное начало модели)."""
        if not self.can_add:
            raise Tv4lError(f"в слой {self.name} нельзя добавить объект: не разобран заголовок "
                            "(добавьте в TB в этот слой хоть один объект и сохраните проект)")
        if not template or not library:
            raise Tv4lError("нет имени шаблона или библиотеки")
        leaf = self._leaf_for(x, z)            # вне границ слоя — ошибка до любых изменений
        h = sdbm(template)
        if h not in {sdbm(n) for n, _ in self._lists[1]}:
            lib_hash = sdbm(library)
            if all(n != library for n, _ in self._lists[0]):
                self._lists[0].append((library, lib_hash))
            names = self._lists[1]
            sorted_before = [n for n, _ in names] == sorted(n for n, _ in names)
            names.append((template, lib_hash))
            if sorted_before:
                names.sort(key=lambda e: e[0])
            self.template_names[h] = template
        oid = int(self.id_counter)
        self.id_counter = oid + 1
        floats = struct.pack("<5f", float(yaw) % 360.0, float(pitch), float(roll), float(scale),
                             float(elevation))
        obj = LayerObject(oid, h, float(x), float(z), floats, self.name, bytes(self._tail))
        for g in leaf.groups:
            if g.template == h:
                g.objects.append(obj)
                break
        else:
            leaf.groups.append(Group(h, [obj]))
        self._header_dirty = True
        return obj

    def remove(self, obj: LayerObject) -> None:
        found = self._leaf_of(obj)
        if found is None:
            raise Tv4lError("объект не из этого слоя")
        leaf, group = found
        group.objects = [o for o in group.objects if o is not obj]
        if not group.objects:
            leaf.groups = [g for g in leaf.groups if g is not group]
        self._header_dirty = True

    def move(self, obj: LayerObject, x: float, z: float, yaw: float | None = None,
             elevation: float | None = None) -> None:
        """Переставить объект слоя (x/z — координаты слоя). Переносит в нужный лист дерева."""
        found = self._leaf_of(obj)
        if found is None:
            raise Tv4lError("объект не из этого слоя")
        target = self._leaf_for(x, z)
        leaf, group = found
        obj.x, obj.z = float(x), float(z)
        if yaw is not None:
            obj.set_yaw(yaw)
        if elevation is not None:
            obj.set_elevation(elevation)
        if target is leaf:
            return
        group.objects = [o for o in group.objects if o is not obj]
        if not group.objects:
            leaf.groups = [g for g in leaf.groups if g is not group]
        for g in target.groups:
            if g.template == obj.template:
                g.objects.append(obj)
                break
        else:
            target.groups.append(Group(obj.template, [obj]))

    # ---- запись
    def _write_node(self, n: Node, out: bytearray) -> None:
        out += bytes([n.kind]) + struct.pack("<4dI", *n.bbox, n.depth) + n.y + bytes([n.mask])
        for bit in sorted(n.children):
            c = n.children[bit]
            if isinstance(c, Leaf):
                out += struct.pack("<4d", *c.bbox) + c.y + c.a + struct.pack("<I", len(c.groups))
                for g in c.groups:
                    out += struct.pack("<II", len(g.objects), g.template)
                    for o in g.objects:
                        out += struct.pack("<2d", o.x, o.z) + o.floats + struct.pack("<I", o.id) + o.tail
            else:
                self._write_node(c, out)

    def _list_bytes(self, items: list[tuple[str, int]]) -> bytes:
        f = self._field_ids
        body = bytearray(struct.pack("<I", len(items)))
        for name, value in items:
            raw = name.encode("utf-8")
            body += struct.pack("<HB", f["pair"], 0x0F)
            body += struct.pack("<HBH", f["key"], 0x0B, len(raw)) + raw
            body += struct.pack("<HBI", f["data"], 0x07, value & 0xFFFFFFFF)
        return struct.pack("<HBI", f["libs"], TYPE_BLOCK, len(body)) + bytes(body)

    def to_bytes(self) -> bytes:
        tree = bytearray()
        self._write_node(self.root, tree)
        d = self._data
        tree_block = struct.pack("<II", len(tree) + 4, len(tree)) + tree
        if not self._header_dirty or not self._spans:
            head = d[:self._tree_at + 3]
            return bytes(head + tree_block + d[self._tree_end:])
        out = bytearray(d[:self._spans[0][2]])
        n_list = 0
        for name, t, start, vstart, vend in self._spans:
            if name == "tree" and start == self._tree_at:
                out += d[start:vstart] + tree_block
            elif name == "objectCount" and t == 0x07:
                out += d[start:vstart] + struct.pack("<I", len(self.objects))
            elif name == "mobjectIDcounter" and self.id_counter is not None and t in (0x05, 0x06, 0x07):
                out += d[start:vstart] + struct.pack("<i" if t != 0x07 else "<I", self.id_counter)
            elif name == "libs" and t == TYPE_BLOCK and n_list < len(self._lists):
                out += self._list_bytes(self._lists[n_list])
                n_list += 1
            else:
                out += d[start:vend]
        return bytes(out)

    def save(self, path: str | Path) -> Path:
        """Записать копию слоя. Поверх исходного файла не пишет."""
        p = Path(path)
        if self.path is not None and p.exists() and p.resolve() == self.path.resolve():
            raise Tv4lError("исходный слой не перезаписываем — укажите другой файл")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(self.to_bytes())
        tmp.replace(p)
        return p


def find_layers_dir(project_dir: str | Path, max_depth: int = 2) -> Path | None:
    """Папка ``<Проект>.Layers`` (со слоями *.tv4l) в папке проекта или рядом."""
    root = Path(project_dir)
    if not root.is_dir():
        return None
    if root.name.lower().endswith(".layers"):
        return root
    from .tbproject import find_project_file
    project = find_project_file(root)
    if project is not None:               # слои того проекта, с которым работает TB
        own = project.with_name(project.stem + ".Layers")
        if not own.is_dir():
            own = next((c for c in project.parent.iterdir()
                        if c.is_dir() and c.name.lower() == own.name.lower()), own)
        if own.is_dir() and any(own.glob("*.tv4l")):
            return own
    level = [root]
    for _ in range(max_depth):
        nxt = []
        for d in level:
            try:
                kids = sorted((c for c in d.iterdir() if c.is_dir()), key=lambda c: c.name.lower())
            except OSError:
                continue
            for c in kids:
                if c.name.lower().endswith(".layers") and any(c.glob("*.tv4l")):
                    return c
            nxt.extend(kids)
        level = nxt
    return None


def load_layers(folder: str | Path) -> list[Tv4lLayer]:
    out = []
    for p in sorted(Path(folder).glob("*.tv4l"), key=lambda p: p.name.lower()):
        try:
            out.append(Tv4lLayer.load(p))
        except (OSError, Tv4lError, struct.error):
            continue
    return out


def _hex(data: bytes, start: int, end: int) -> list[str]:
    start, end = max(0, start), min(len(data), end)
    start -= start % 16
    return [f"  {o:08x}: " + data[o:min(o + 16, end)].hex(" ") for o in range(start, end, 16)]


def diagnose(data: bytes) -> list[str]:
    """Подробности о файле слоя, который не разбирается (для лога): поля заголовка, где лежит
    дерево, последние прочитанные узлы и байты вокруг места сбоя."""
    from .tbproject import AlbError, AlbReader
    lines = [f"размер {len(data)} байт, начало {data[:4]!r}"]
    if data[:4] != MAGIC:
        return lines
    try:
        reader = AlbReader(data)
        lines.append("поля: " + ", ".join(f"{n}(0x{i:x})" for i, n in sorted(reader.fields.items())))
        lines.append("классы: " + ", ".join(f"{n}(0x{i:x})" for i, n in sorted(reader.classes.items())))
    except (AlbError, struct.error, IndexError, UnicodeDecodeError) as exc:
        lines.append(f"таблицы имён не разобраны: {exc}")
        return lines + _hex(data, 0, 256)
    try:
        spans = reader.top_spans()
        parts = []
        for name, t, start, vstart, vend in spans:
            text = f"{name}:0x{t:02x}@{start}+{vend - vstart}"
            if t in (0x05, 0x06, 0x07) and vend - vstart == 4:
                text += "=" + str(struct.unpack_from("<i" if t != 0x07 else "<I", data, vstart)[0])
            elif t == 0x0B and vend - vstart <= 66:
                text += "=" + data[vstart + 2:vend].decode("latin-1")
            parts.append(text)
        lines.append("заголовок: " + " ".join(parts))
    except (AlbError, struct.error, IndexError, UnicodeDecodeError) as exc:
        lines.append(f"поля заголовка не разобраны: {exc}")
    tid = {v: k for k, v in reader.fields.items()}.get("tree")
    if tid is not None:
        for code in range(0x01, 0x20):
            tag = struct.pack("<HB", tid, code)
            at = data.find(tag, reader.body)
            hits = 0
            while at >= 0 and hits < 3:
                if at + 11 <= len(data):
                    size, inner = struct.unpack_from("<II", data, at + 3)
                    if size <= len(data):
                        lines.append(f"tree тип 0x{code:02x} @{at}: размер {size}, внутри {inner}")
                        if code == TYPE_BLOCK:
                            lines += _hex(data, at, at + 96)
                        hits += 1
                at = data.find(tag, at + 1)
    try:
        layer = Tv4lLayer(data)
        lines.append(f"разобран: {len(layer.objects)} объектов")
    except Tv4lError as exc:
        lines.append(f"ошибка: {exc}")
        if exc.trail:
            lines.append("последние узлы (смещение, что, вид/групп, глубина, маска): " + "; ".join(
                f"{a} {w} 0x{k:02x} {d} {m:04b}" for a, w, k, d, m in exc.trail))
        if exc.pos is not None:
            lines.append(f"байты вокруг смещения {exc.pos}:")
            lines += _hex(data, exc.pos - 96, exc.pos + 96)
    except (struct.error, IndexError, ValueError) as exc:
        lines.append(f"ошибка: {exc}")
    return lines
