"""Объекты, уже стоявшие на карте: строки моста v10, правки, сдвиг координат слоя, копии слоёв."""
import re

from terrain_editor import buldozer_bridge as bb
from terrain_editor.map_edits import (MapEdit, Sample, _by_template, angle_delta, edited_dir,
                                      find_offset, norm_model, save_edits, template_hashes)
from terrain_editor.tv4l import Tv4lLayer, sdbm

from test_tv4l import make_layer

OX, OZ = 500.0, 300.0          # «сдвиг проекта»: координаты слоя = Buldozer + (OX, OZ)


def names(model):
    return {"evelone\\ev_1.p3d": ["EV_1"], "evelone\\ev_2.p3d": ["EV_2"]}.get(norm_model(model), [])


def layer_bytes():
    # EV_1 — два одинаковых (стены), EV_2 — один (редкий)
    return make_layer([(sdbm("EV_1"), [(100.0, 900.0, 90.0, 1), (110.0, 910.0, 90.0, 2)])],
                      [(sdbm("EV_2"), [(900.0, 100.0, 180.0, 3)])])


def test_norm_model_and_hashes():
    assert norm_model("P:\\Evelone\\EV_1.p3d") == "evelone\\ev_1.p3d" == norm_model("/evelone/ev_1.p3d")
    assert template_hashes("evelone\\ev_1.p3d", names) == {sdbm("EV_1"), sdbm("ev_1")}
    assert template_hashes("dz\\rock_1.p3d", None) == {sdbm("rock_1")}
    assert angle_delta(10, 350) == 20 and angle_delta(350, 10) == -20


def test_offset_needs_unambiguous_objects():
    index = _by_template([Tv4lLayer(layer_bytes())])
    wall = Sample("evelone\\ev_1.p3d", 100.0 - OX, 900.0 - OZ)
    rare = Sample("evelone\\ev_2.p3d", 900.0 - OX, 100.0 - OZ)
    off, why = find_offset([wall], index, names)
    assert off is None and "ещё" in why                      # стен две — непонятно, какая
    off, _ = find_offset([rare], index, names)
    assert off == (OX, OZ)                                   # редкая модель — сразу ясно
    off, why = find_offset([wall, rare], index, names)
    assert off == (OX, OZ) and "2" in why
    off, why = find_offset([Sample("dz\\other.p3d", 1, 2)], index, names)
    assert off is None and "не найден" in why


def test_save_edits_moves_object_by_delta_in_a_copy(tmp_path):
    src = tmp_path / "Evelone2666.Layers"
    src.mkdir()
    (src / "Walls.tv4l").write_bytes(layer_bytes())
    layers = [Tv4lLayer.load(src / "Walls.tv4l")]
    e = MapEdit("evelone\\ev_1.p3d", 110.0 - OX, 910.0 - OZ, 90.0, x=115.0 - OX, z=905.0 - OZ, yaw=120.0,
                lift=0.5)
    rep = save_edits(layers, [e], edited_dir(src, 0), names,
                     samples=[Sample("evelone\\ev_2.p3d", 900.0 - OX, 100.0 - OZ)])
    assert rep.moved == 1 and rep.offset == (OX, OZ) and rep.lifted == 1 and not rep.not_found
    assert rep.project is None
    out = rep.written[0]
    assert out.parent.parent.name == "WE_edited_layers" and out.name == "Walls.tv4l"
    moved = Tv4lLayer.load(out).find(2)
    assert (moved.x, moved.z) == (115.0, 905.0) and abs(moved.yaw - 120.0) < 1e-4
    assert abs(moved.elevation - 1.5) < 1e-5            # было 1.0 — подъём в поле Relative
    assert Tv4lLayer.load(out).find(1).x == 100.0             # соседняя стена на месте
    assert (src / "Walls.tv4l").read_bytes() == layer_bytes()  # оригинал не тронут
    assert e.saved


def test_save_edits_reports_unclear_offset_and_missing(tmp_path):
    layers = [Tv4lLayer(layer_bytes())]
    e = MapEdit("evelone\\ev_1.p3d", 100.0 - OX, 900.0 - OZ, 90.0, x=101.0 - OX, z=900.0 - OZ, yaw=90.0)
    rep = save_edits(layers, [e], tmp_path / "out", names)
    assert not rep.written and "ещё" in rep.message
    gone = MapEdit("evelone\\ev_2.p3d", 1.0, 2.0, 0.0, x=3.0, z=2.0, yaw=0.0)
    rep = save_edits(layers, [gone], tmp_path / "out", names, offset=(OX, OZ))
    assert not rep.written and rep.not_found == [gone]
    rep = save_edits(layers, [MapEdit("x.p3d", 1, 2, 0, x=1, z=2, yaw=0)], tmp_path / "out", names)
    assert "нет изменённых" in rep.message


# ------------------------------------------------------------------ мост: строки v10

def test_state_lines_v10():
    text = ("WE_BRIDGE 1\nseq 9\ngen 4\nbs 123456\nH|evelone\\ev_1.p3d\n"
            "P|77|123456|5|-1|evelone\\ev_1.p3d|1000500|20000|2000250|90000|0|0|1000|1000000|2000000|"
            "45000|1500\nDP|123456|5|1002000|2003500\nend\n")
    st = bb.parse_state(text)
    assert st.session == 123456 and st.hover == "evelone\\ev_1.p3d"
    p = st.pick
    assert p.found and (p.seq, p.session, p.ref, p.own) == (77, 123456, 5, -1)
    assert p.pos == (1000.5, 20.0, 2000.25) and p.ori == (90.0, 0.0, 0.0) and p.scale == 1.0
    assert (p.x0, p.z0, p.yaw0, p.rel0) == (1000.0, 2000.0, 45.0, 1.5)
    assert st.drag == (123456, 5, 1002.0, 2003.5)
    nothing = bb.parse_state("WE_BRIDGE 1\nseq 1\nP|78|123456|-1\nend\n").pick
    assert nothing.seq == 78 and not nothing.found


def test_format_commands_extra_lines():
    text = bb.format_commands(5, [], extra=["K|9", "T|1|2|10|20|90|0", "S|1|2"])
    lines = text.splitlines()
    assert lines[-4:] == ["K|9", "T|1|2|10|20|90|0", "S|1|2", "end"]


def test_session_keeps_map_edits_and_samples(tmp_path):
    s = bb.PlacementSession(tmp_path / "s.json")
    pick = bb.parse_pick("P|1|42|3|-1|ev\\a.p3d|10000|0|20000|370000|0|0|1000|10000|20000|370000|0".split("|"))
    e = s.edit_map_object(pick, 42)
    assert (e.x0, e.z0, e.yaw0, e.ref, e.sess) == (10.0, 20.0, 10.0, 3, 42) and not e.changed
    e.x += 2
    assert e.changed and e.command() == "T|42|3|12|20|10|0"
    s.add_sample("ev\\a.p3d", 10.0, 20.0)
    s.add_sample("ev\\a.p3d", 10.0, 20.0)                  # тот же объект — один раз
    s.save()
    again = bb.PlacementSession(tmp_path / "s.json")
    again.load()
    assert len(again.map_edits) == 1 and again.map_edits[0].x == 12.0 and len(again.samples) == 1
    # Buldozer перезапустили: тот же объект (там же, где стоял) — та же правка, новый номер
    pick2 = bb.parse_pick("P|5|99|1|-1|EV\\A.p3d|10000|0|20000|10000|0|0|1000|10000|20000|10000|0".split("|"))
    e2 = again.edit_map_object(pick2, 99)
    assert e2 is again.map_edits[0] and (e2.sess, e2.ref) == (99, 1) and e2.x == 12.0


# ------------------------------------------------------------------ скрипт v10

def _functions(block):
    """(имя, тело) функций класса моста — грубо, по отступу в один таб."""
    out = []
    for m in re.finditer(r"^\t(?:protected |static |override )*[\w<>, ]+ (\w+)\([^)]*\)\n\t\{\n", block, re.M):
        start = m.end()
        end = block.index("\n\t}\n", start)
        out.append((m.group(1), block[start:end]))
    return out


DECL = re.compile(r"(?<![\w.<])(?:ref\s+)?(?:int|float|bool|string|vector|Object|Shape|Input|"
                  r"UIManager|UIMenuPanel|UIScriptedMenu|FileHandle|array<\w+>|map<\w+, \w+>|set<\w+>)"
                  r"\s+(\w+)\s*(?=[=;\[:,)])")


def test_script_v10_compiles_plausibly():
    block = bb.script_block()
    assert block.count("{") == block.count("}") and block.count("(") == block.count(")")
    funcs = _functions(block)
    assert len(funcs) > 30
    for name, body in funcs:
        seen = DECL.findall(body)
        dup = {v for v in seen if seen.count(v) > 1}
        assert not dup, f"{name}: повторное объявление {dup}"


def test_script_v10_pick_move_select():
    block = bb.script_block()
    assert bb.SCRIPT_REVISION >= 10 and f"bridge v{bb.SCRIPT_REVISION}" in block
    pick = block[block.index("protected Object PickObject("):block.index("protected string ModelOf(")]
    assert "RaycastRV(from, to, contact, normal, component, found, NULL, m_Ghost, true, false, how)" in pick
    assert "ObjIntersectView" in pick and "ObjIntersectGeom" in pick
    tick = block[block.index("\tvoid Tick()"):block.index("protected Object PickObject(")]
    assert tick.index("MoveGhost(hit);") < tick.index("PickObject(camPos, rayEnd)") < tick.index("WriteState(")
    assert "RegisterPick(under)" in tick and "DragTo(hit);" in tick and "UpdateSelShape();" in tick
    reg = block[block.index("protected string RegisterPick("):block.index("protected void ApplyTransforms(")]
    assert "m_PickPos.Set(pr, o.GetPosition());" in reg and "m_PickOri.Set(pr, o.GetOrientation());" in reg
    # поля строки P — те же, что разбирает parse_pick
    assert reg.count('+ "|" +') + reg.count('"|" +') >= 10
    cmds = block[block.index("protected void ReadCommands()"):block.index("protected bool SameShape(")]
    for key in ('parts.Get(0) == "K"', 'parts.Get(0) == "T" && parts.Get(1) == sess',
                'parts.Get(0) == "D" && parts.Get(1) == sess', 'parts.Get(0) == "S" && parts.Get(1) == sess'):
        assert key in cmds
    assert cmds.index("SetDrag(dragRef);") < cmds.index("ApplyTransforms(moves);")
    assert "SameShape(oldLine, newLine)" in cmds and "PositionPlaced(shiftId" in cmds  # свой — без пересоздания
    place = block[block.index("protected void PlacePicked("):block.index("protected void RestorePicked(")]
    assert "RelHeight(m_PickPos.Get(pr))" in place and "ori[1], ori[2]" in place      # тангаж/крен как были
    assert "Shape.Create(ShapeType.BBOX" in block and "m_SelShape.SetMatrix(m);" in block
    destructor = block[block.index("void ~WE_BuldozerBridge()"):block.index("protected void EnsureMaps()")]
    assert "RestoreAllPicked();" in destructor                      # F10 ещё раз — объекты карты на место
    for line in ('"bs " + m_Sess.ToString()', '"H|" + m_Hover', "FPrintln(file, m_PickResult);",
                 "FPrintln(file, m_DragResult);"):
        assert line in block
