import pytest

from terrain_editor.objects import ObjectListError, parse_line, parse_objects


def test_x_offset_is_removed():
    o = parse_line('"metalcase_01";200248.5;1500.25;90;0;0;1;3.5', 1)
    assert o.model == "metalcase_01"
    assert abs(o.x - 248.5) < 1e-6
    assert abs(o.z - 1500.25) < 1e-6
    assert o.yaw == 90 and o.scale == 1 and abs(o.y - 3.5) < 1e-6


def test_small_x_without_offset():
    o = parse_line('"a";5.0;10;0;0;0;1;0', 1)
    assert o.x == 5.0


def test_parse_list_skips_bad_lines():
    text = '"a";200100;200;0;0;0;1;0\n\nbroken line\n"b";300;400;10;0;0;1;2\n'
    objs, errs = parse_objects(text)
    assert [o.model for o in objs] == ["a", "b"]
    assert len(errs) == 1 and "quoted" in errs[0]


def test_wrong_field_count_is_error():
    with pytest.raises(ObjectListError):
        parse_line('"a";1;2;3', 1)
