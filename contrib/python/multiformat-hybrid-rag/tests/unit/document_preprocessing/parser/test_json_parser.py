import json

from src.document_preprocessing.parser.json_parser import (
    _heading_for,
    _is_empty,
    _load_records,
    _render,
    _render_array,
    _render_object,
    parse,
)


class TestIsEmpty:
    def test_none(self):
        assert _is_empty(None) is True

    def test_empty_string(self):
        assert _is_empty("") is True

    def test_empty_list(self):
        assert _is_empty([]) is True

    def test_empty_dict(self):
        assert _is_empty({}) is True

    def test_non_empty_string(self):
        assert _is_empty("hello") is False

    def test_non_empty_list(self):
        assert _is_empty([1]) is False

    def test_non_empty_dict(self):
        assert _is_empty({"a": 1}) is False

    def test_zero_is_not_empty(self):
        assert _is_empty(0) is False

    def test_false_is_not_empty(self):
        assert _is_empty(False) is False


class TestHeadingFor:
    def test_picks_name_field(self):
        assert _heading_for({"name": "Alpha", "title": "Beta"}, 1) == "Alpha"

    def test_picks_title_when_no_name(self):
        assert _heading_for({"title": "Beta", "id": "C"}, 1) == "Beta"

    def test_picks_id_when_no_name_or_title(self):
        assert _heading_for({"id": "C123"}, 1) == "C123"

    def test_fallback_to_item_n(self):
        assert _heading_for({"other": "value"}, 3) == "Item 3"

    def test_skips_empty_name(self):
        assert _heading_for({"name": "", "title": "Real"}, 1) == "Real"

    def test_numeric_id(self):
        assert _heading_for({"id": 42}, 1) == "42"


class TestRender:
    def test_primitive_string(self):
        assert _render("hello world") == "hello world"

    def test_primitive_int(self):
        assert _render(42) == "42"

    def test_dict(self):
        result = _render({"key": "value"})
        assert "**key**: value" in result

    def test_list_of_objects(self):
        data = [{"name": "A", "x": 1}, {"name": "B", "x": 2}]
        result = _render(data)
        assert "## A" in result
        assert "## B" in result
        assert "---" in result

    def test_list_of_primitives(self):
        result = _render(["a", "b", "c"])
        assert "- a" in result
        assert "- b" in result
        assert "- c" in result

    def test_empty_value_returns_empty(self):
        assert _render(None) == ""
        assert _render("") == ""
        assert _render([]) == ""
        assert _render({}) == ""

    def test_nested_objects(self):
        data = {"outer": {"inner": "val"}}
        result = _render(data)
        assert "outer" in result
        assert "**inner**: val" in result

    def test_newlines_collapsed_in_primitives(self):
        assert _render("line1\nline2") == "line1 line2"


class TestRenderObject:
    def test_scalar_fields(self):
        result = _render_object({"name": "Alice", "age": 30}, depth=0)
        assert "**name**: Alice" in result
        assert "**age**: 30" in result

    def test_nested_dict_as_subsection(self):
        result = _render_object({"info": {"city": "Rome"}}, depth=0)
        assert "## info" in result
        assert "**city**: Rome" in result

    def test_nested_list_as_subsection(self):
        result = _render_object({"tags": ["a", "b"]}, depth=0)
        assert "## tags" in result
        assert "- a" in result

    def test_skips_empty_fields(self):
        result = _render_object(
            {"name": "X", "empty": None, "blank": ""}, depth=0
        )
        assert "**name**: X" in result
        assert "empty" not in result
        assert "blank" not in result

    def test_heading_depth_caps_at_h6(self):
        result = _render_object({"nested": {"a": 1}}, depth=5)
        assert "######" in result
        assert "#######" not in result


class TestRenderArray:
    def test_array_of_objects_with_headings(self):
        items = [{"name": "A", "val": 1}, {"name": "B", "val": 2}]
        result = _render_array(items, depth=0)
        assert "## A" in result
        assert "## B" in result

    def test_array_of_objects_separated_by_hr(self):
        items = [{"name": "A", "val": 1}, {"name": "B", "val": 2}]
        result = _render_array(items, depth=0)
        assert "---" in result

    def test_array_of_primitives_as_bullets(self):
        result = _render_array(["x", "y", "z"], depth=0)
        assert "- x" in result
        assert "- y" in result
        assert "- z" in result

    def test_empty_array(self):
        assert _render_array([], depth=0) == ""


class TestParse:
    def test_json_file(self, tmp_path):
        data = [{"name": "Item1", "value": 10}]
        f = tmp_path / "data.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        result = parse(str(f))
        assert "## Item1" in result
        assert "**value**: 10" in result

    def test_jsonl_file(self, tmp_path):
        lines = [
            json.dumps({"name": "A", "x": 1}),
            json.dumps({"name": "B", "x": 2}),
        ]
        f = tmp_path / "data.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        result = parse(str(f))
        assert "## A" in result
        assert "## B" in result

    def test_single_object_json(self, tmp_path):
        data = {"title": "Doc", "body": "content"}
        f = tmp_path / "single.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        result = parse(str(f))
        assert "**title**: Doc" in result
        assert "**body**: content" in result


class TestLoadRecords:
    def test_json_file(self, tmp_path):
        data = {"key": "value"}
        f = tmp_path / "test.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        result = _load_records(f)
        assert result == {"key": "value"}

    def test_jsonl_file(self, tmp_path):
        lines = ['{"a":1}', '{"b":2}']
        f = tmp_path / "test.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        result = _load_records(f)
        assert result == [{"a": 1}, {"b": 2}]

    def test_jsonl_skips_malformed_line(self, tmp_path, caplog):
        lines = ['{"a":1}', "NOT JSON", '{"b":2}']
        f = tmp_path / "bad.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        result = _load_records(f)
        assert len(result) == 2
        assert result[0] == {"a": 1}
        assert result[1] == {"b": 2}

    def test_jsonl_skips_empty_lines(self, tmp_path):
        lines = ['{"a":1}', "", '{"b":2}', "  "]
        f = tmp_path / "gaps.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        result = _load_records(f)
        assert len(result) == 2
