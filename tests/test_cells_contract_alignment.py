"""Feature 5 contract cases, paired with the other repository's same-named file.

Core creates standalone Cells; workflow binds roots and derived conversions
into a real Context. Cases are paired; fixtures and known-gap marks differ.
Known gaps assert the intended result under non-strict xfail, never the bug.
"""
import asyncio
import sys

import pytest

from seamless import Buffer, CacheMissError, Cell, Checksum, Expression
from seamless.checksum.celltypes import celltypes
from seamless.cell_errors import AuthorityError, ProjectionError
from seamless.checksum.hash_type_validation import HashTypeValidationError


def gap(reason):
    return pytest.mark.xfail(strict=False, reason="cells.md contract ahead of code: " + reason)


@pytest.fixture
def world(make_context):
    class World:
        bound = True
        serial = 0

        def __init__(self):
            self.ctx = make_context()

        def make(self, *args, **kwargs):
            return self.bind(Cell(*args, **kwargs))

        def bind(self, cell):
            self.serial += 1
            name = f"cell_{self.serial}"
            setattr(self.ctx, name, cell)
            return getattr(self.ctx, name)

        def settle(self, *cells):
            self.ctx.compute(timeout=10)

    return World()


def write(cell, form, method, value):
    if method:
        getattr(cell, {"value": "set", "buffer": "set_buffer", "checksum": "set_checksum"}[form])(value)
    else:
        setattr(cell, form, value)


@pytest.mark.parametrize("form", ["value", "buffer", "checksum"])
@pytest.mark.parametrize("method", [False, True], ids=["declare", "owned"])
def test_root_write_matrix(world, form, method):
    cell = world.make("int")
    buffer = Buffer(73, "int")
    hold = buffer.tempref()
    try:
        value = {"value": 73, "buffer": buffer, "checksum": buffer.get_checksum()}[form]
        write(cell, form, method, value)
        world.settle(cell)
        assert cell.value == 73
        assert cell.checksum == buffer.get_checksum()
        assert cell.input_celltype == cell.celltype == "int"
        assert cell.source is None
    finally:
        hold.clear()


@pytest.mark.parametrize("form", ["value", "buffer", "checksum"])
def test_connected_methods_refuse_and_properties_detach(world, form):
    source = world.make("int")
    source.set(3)
    cell = world.make("int", source=source)
    buffer = Buffer(79, "int")
    hold = buffer.tempref()
    try:
        value = {"value": 79, "buffer": buffer, "checksum": buffer.get_checksum()}[form]
        with pytest.raises(AuthorityError):
            write(cell, form, True, value)
        write(cell, form, False, value)
        source.set(5)
        world.settle(source, cell)
        assert cell.source is None
        assert cell.value == 79
        assert source.value == 5
    finally:
        hold.clear()


@pytest.mark.parametrize("method", [False, True])
@pytest.mark.parametrize("kind", ["checksum", "cell", "expression"])
def test_value_writes_reject_references_without_changing_input(world, method, kind):
    source = world.make("int")
    source.set(7)
    world.settle(source)
    cell = world.make("int")
    cell.set(19)
    value = {"checksum": source.checksum, "cell": source, "expression": source.build()}[kind]
    with pytest.raises(TypeError, match="not a value"):
        write(cell, "value", method, value)
    world.settle(cell)
    assert cell.value == 19


@pytest.mark.parametrize("form", ["value", "buffer"])
def test_invalid_literal_write_is_eager_and_atomic(world, form):
    cell = world.make("int")
    cell.set(23)
    invalid = "not an integer" if form == "value" else Buffer(b"not an integer")
    with pytest.raises((ValueError, HashTypeValidationError)):
        write(cell, form, False, invalid)
    world.settle(cell)
    assert cell.value == 23


@pytest.mark.parametrize("clear_form", ["checksum", "buffer", "set_checksum"])
def test_null_is_complete_clear_is_unwired(world, clear_form):
    cell = world.make("plain")
    cell.set(None)
    world.settle(cell)
    assert cell.state == "complete"
    assert cell.checksum == Buffer(None, "plain").get_checksum()
    assert cell.value is None
    if clear_form == "set_checksum":
        cell.set_checksum(None)
    else:
        setattr(cell, clear_form, None)
    assert cell.state == "unwired"
    assert cell.checksum is None
    assert cell.input_celltype is None
    cell.value = None
    world.settle(cell)
    assert cell.state == "complete"


@pytest.mark.parametrize("form", ["value", "buffer", pytest.param("checksum", marks=gap("observed 2026-09-22: bound dummy bytes checksum does not canonicalize empty to null"))])
def test_empty_bytes_canonicalizes_to_null(world, form):
    cell = world.make("bytes")
    buffer = Buffer(b"")
    hold = buffer.tempref()
    try:
        write(cell, form, False, {"value": b"", "buffer": buffer, "checksum": buffer.get_checksum()}[form])
        world.settle(cell)
        assert cell.checksum == Buffer(None, "plain").get_checksum()
        assert cell.value == cell.run() == b""
    finally:
        hold.clear()


@pytest.mark.parametrize("method", [False, True])
def test_checksum_celltype_treats_checksum_as_value(world, method):
    digest = Checksum("ab" * 32)
    cell = world.make("checksum")
    write(cell, "value", method, digest)
    world.settle(cell)
    assert isinstance(cell.value, Checksum)
    assert cell.value == cell.run() == cell.build().run() == digest
    assert cell.buffer.content == digest.hex().encode()
    by_hex = world.make("checksum")
    by_hex.set(digest.hex())
    world.settle(by_hex)
    assert by_hex.checksum == cell.checksum


def test_retyping_preserves_input_declaration(world):
    cell = world.make("str")
    cell.set("hello")
    world.settle(cell)
    old_checksum = cell.checksum
    cell.celltype = "text"
    world.settle(cell)
    assert cell.input_celltype == "str"
    assert cell.celltype == "text"
    assert cell.value == "hello"
    assert cell.checksum != old_checksum
    assert cell.checksum == Buffer("hello", "text").get_checksum()
    with pytest.raises(AttributeError):
        cell.input_celltype = "text"


def test_typed_source_type_is_live_output_is_copied_once(world):
    source = world.make("int")
    source.set(41)
    cell = world.make(source=source)
    assert cell.celltype == "int"
    source.celltype = "float"
    world.settle(source, cell)
    assert cell.input_celltype == "float"
    assert cell.celltype == "int"
    assert cell.value == 41


def test_declared_checksum_can_convert_but_property_write_uses_output_type(world):
    buffer = Buffer(43, "int")
    hold = buffer.tempref()
    try:
        cell = world.make("str", checksum=buffer.get_checksum(), input_celltype="int")
        world.settle(cell)
        assert cell.input_celltype == "int"
        assert cell.value == "43"
        cell.value = "47"
        world.settle(cell)
        assert cell.input_celltype == "str"
        assert cell.value == "47"
    finally:
        hold.clear()


@gap("constructor path= must be removed (Appendix F.1a)")
def test_constructor_rejects_path(world):
    with pytest.raises(TypeError, match="path"):
        world.make("plain", path="a")


@pytest.mark.parametrize("name", ["path", "path_python"])
@pytest.mark.parametrize("projection", [False, True])
def test_path_is_readonly(world, name, projection):
    root = world.make("plain")
    root.set({"a": 1})
    cell = root["a"] if projection else root
    original = cell.path
    with pytest.raises(AttributeError):
        setattr(cell, name, "b")
    assert cell.path == cell.path_python == original


@pytest.mark.parametrize("operation", [
    lambda c: c == 1, lambda c: c != 1, lambda c: c < 1,
    lambda c: c <= 1, lambda c: c > 1, lambda c: c >= 1,
    bool, len, iter, lambda c: c == Cell("plain"),
], ids=["eq", "ne", "lt", "le", "gt", "ge", "bool", "len", "iter", "cell-eq"])
@pytest.mark.parametrize("projection", [pytest.param(False, marks=gap("root guards must move to CellBase")), True])
def test_handle_value_operations_raise_projection_error(world, operation, projection):
    root = world.make("plain")
    root.set({"a": [1, 2]})
    cell = root["a"] if projection else root
    with pytest.raises(ProjectionError):
        operation(cell)


@gap("SubCell class and exports must be removed (Appendix F.1a)")
def test_projection_is_a_cell_without_subcell_export(world):
    import seamless
    root = world.make("plain")
    child = root["a"]
    assert type(child) is Cell
    assert "SubCell" not in seamless.__all__
    assert not hasattr(seamless, "SubCell")


def test_navigation_links_to_parent_and_fuses_paths(world):
    root = world.make("plain")
    root.set({"a": {"b": 53}})
    first = root["a"]
    child = first["b"]
    world.settle(root, child)
    if not world.bound:
        assert first.source is root
        assert child.source is first
    assert root.path == ""
    assert root.value == {"a": {"b": 53}}
    assert child.value == 53
    assert child.build().identity_key == Expression(root.checksum, input_celltype="plain", celltype="plain", path="a.b").identity_key


@gap("syntax order is application order; conversion closes and rebases the chain")
def test_conversion_before_projection_differs_from_projection_before_conversion(world):
    root = world.make("text")
    root.set("[10, 20, 30, 40]")
    before = world.bind(root.as_celltype("plain")[3])
    after = world.bind(root[3].as_celltype("plain"))
    world.settle(root, before, after)
    assert before.value == 40
    assert after.value == ","
    assert before.build().identity_key != after.build().identity_key


@gap("one link may project OR convert; invalid rewiring must raise")
@pytest.mark.parametrize("api", ["constructor", "with_input"])
def test_projected_source_cannot_implicitly_convert(world, api):
    source = world.make("text")
    source.set("[10, 20]")
    target = world.make("plain")
    target.set({"unchanged": True})
    world.settle(source, target)
    with pytest.raises(TypeError, match="project"):
        if api == "constructor":
            world.make("plain", source=source[3])
        else:
            target.with_input(source[3])
    assert target.value == {"unchanged": True}


@gap("retyping a projecting consumer must refuse a conversion behind its path")
def test_projection_cannot_be_retyped_in_place(world):
    root = world.make("plain")
    root.set({"a": 59})
    child = root["a"]
    with pytest.raises(TypeError):
        child.celltype = "text"
    assert child.celltype == "plain"


@gap("source retyping makes projecting consumers miswired, including standalone")
def test_source_retype_marks_consumer_miswired(world):
    source = world.make("text")
    source.set("[10, 20]")
    child = world.bind(source[3])
    world.settle(source, child)
    assert child.value == ","
    source.celltype = "plain"
    world.settle(source, child)
    assert child.state == "miswired"
    assert child.checksum is None
    source.celltype = "text"
    world.settle(source, child)
    assert child.state == "complete"
    assert child.value == ","


@gap("binding a Cell whose source is an Expression is currently rejected (Connecting)")
def test_read_evaluates_upstream_expression(world):
    source = Buffer({"a": 61}, "plain")
    hold = source.tempref()
    try:
        expression = Expression(source.get_checksum(), input_celltype="plain", celltype="plain", path="a")
        cell = world.make("plain", source=expression)
        if world.bound:
            world.settle(cell)
        assert cell.checksum == Buffer(61, "plain").get_checksum()
        assert cell.value == 61
    finally:
        hold.clear()


@gap("Cell.exception becomes a string, not an exception object")
def test_failure_is_a_stable_string_and_new_input_recovers(world):
    cell = world.make("str")
    cell.set("cannot parse as integer")
    cell.celltype = "int"
    world.settle(cell)
    assert cell.state == "failed"
    assert cell.checksum is None
    error = cell.exception
    assert isinstance(error, str) and error
    assert cell.exception == error
    cell.clear_exception()
    world.settle(cell)
    assert cell.state == "failed"
    assert cell.exception == error
    cell.set(67)
    world.settle(cell)
    assert cell.exception is None
    assert cell.value == 67


@gap("buffer and value must re-raise the same recorded evaluation failure (bug 4)")
def test_buffer_value_report_recorded_failure_consistently(world):
    cell = world.make("str")
    cell.set("not an int")
    cell.celltype = "int"
    world.settle(cell)
    assert cell.state == "failed"
    errors = []
    for attr in ("buffer", "value"):
        with pytest.raises(Exception) as caught:
            getattr(cell, attr)
        errors.append(caught.value)
    assert type(errors[0]) is type(errors[1])
    assert str(errors[0]) == str(errors[1])


@gap("bound missing projections must record evaluation failure, not raise (bug 3)")
def test_missing_projection_compute_reports_failure(world):
    root = world.make("plain")
    root.set({"present": 71})
    world.settle(root)
    missing = root["missing"]
    assert missing.compute() is None
    assert missing.checksum is None
    assert missing.exception is not None
    assert missing.state == "failed"


@gap("bound public reads must validate and record materialization failures (bug 2)")
@pytest.mark.parametrize("attr", ["buffer", "value"])
def test_invalid_result_read_validates_and_records(world, attr):
    source = Buffer(b"not valid JSON")
    hold = source.tempref()
    try:
        cell = world.make("plain", checksum=source.get_checksum())
        with pytest.raises(HashTypeValidationError):
            getattr(cell, attr)
        assert cell.exception is not None
        assert cell.state == "failed"
        assert cell.checksum is None
    finally:
        hold.clear()


@gap("bound public deserialization must record failure; exception becomes a string")
def test_deserialization_failure_is_repeatable_after_clear(world):
    source = Buffer(b"def broken(:\n")
    hold = source.tempref()
    try:
        cell = world.make("python", checksum=source.get_checksum())
        assert cell.buffer.content == source.content
        for _ in range(2):
            with pytest.raises(HashTypeValidationError):
                _ = cell.value
            assert isinstance(cell.exception, str)
            assert cell.state == "failed"
            cell.clear_exception()
    finally:
        hold.clear()


@gap("Cell.fingertip is not implemented; no result must mean no work")
def test_fingertip_without_result_never_evaluates(world, monkeypatch):
    cell = world.make("int")
    def forbidden(*args, **kwargs):
        pytest.fail("fingertip started Cell evaluation without a result")
    monkeypatch.setattr(Expression, "compute", forbidden)
    monkeypatch.setattr(Expression, "compute_async", forbidden)
    assert cell.fingertip() is None
    assert cell.exception is None
    assert cell.state == "unwired"


@pytest.mark.parametrize("celltype", ["deepcell", "deepfolder", "folder"])
def test_deep_value_is_typed_index_without_resolving_members(world, celltype, monkeypatch):
    member = Checksum("ac" * 32)
    source = Buffer({"dir/file": member.hex()}, "plain")
    hold = source.tempref()
    original = Checksum.resolve
    def resolve(checksum, *args, **kwargs):
        assert checksum != member, "deep value read resolved a member"
        return original(checksum, *args, **kwargs)
    monkeypatch.setattr(Checksum, "resolve", resolve)
    try:
        cell = world.make(celltype, checksum=source.get_checksum())
        world.settle(cell)
        value = cell.value
        assert isinstance(value["dir/file"], Checksum)
        assert value["dir/file"] == member
        assert cell.buffer.content == source.content
    finally:
        hold.clear()


def test_snapshot_is_frozen_when_root_input_changes(world):
    root = world.make("plain")
    root.set({"a": 83})
    world.settle(root)
    projected = root["a"]
    snapshot = projected.build()
    root.set({"a": 89})
    world.settle(root, projected)
    assert snapshot.run() == 83
    assert projected.value == 89


@gap("observed 2026-09-22: build/as_celltype on an unwired bound Cell raises ValueError")
def test_configuration_and_builder_methods_do_not_evaluate(world, monkeypatch):
    root = world.make("plain")
    # An unwired root prevents the Context's independent eager work from racing
    # this assertion; only the builder operations are under test.
    def forbidden(*args, **kwargs):
        pytest.fail("configuration access started expression evaluation")
    monkeypatch.setattr(Expression, "compute", forbidden)
    monkeypatch.setattr(Expression, "compute_async", forbidden)
    assert root.celltype == "plain"
    assert root.input_celltype is None
    assert root.source is None
    assert root.path == root.path_python == ""
    _ = root["a"], root[:2], root.as_celltype("mixed"), root.build()


@pytest.mark.parametrize("name", ["target_celltype", "input_ref"])
def test_retired_names_raise_but_item_keys_remain_available(world, name):
    cell = world.make("plain")
    cell.set({name: 97})
    for operation in (lambda: getattr(cell, name), lambda: setattr(cell, name, 1), lambda: delattr(cell, name)):
        with pytest.raises(AttributeError):
            operation()
    selected = cell[name]
    world.settle(cell, selected)
    assert selected.value == 97


def test_api_name_collision_requires_item_navigation(world):
    cell = world.make("plain")
    cell.set({"value": 101, "run": 103, "pins": 107})
    world.settle(cell)
    assert cell.value == {"value": 101, "run": 103, "pins": 107}
    for name, expected in (("value", 101), ("run", 103), ("pins", 107)):
        child = cell[name]
        world.settle(child)
        assert child.value == expected
    assert callable(cell.run)
    pins = cell.pins
    world.settle(pins)
    assert pins.value == 107


@pytest.mark.parametrize("method", ["build", "compute", "run"])
def test_one_shot_input_override_does_not_mutate_cell(world, method):
    cell = world.make("int")
    cell.set(127)
    buffer = Buffer(131, "int")
    hold = buffer.tempref()
    try:
        result = getattr(cell, method)(buffer.get_checksum())
        if method == "build":
            assert result.run() == 131
        elif method == "compute":
            assert result == buffer.get_checksum()
        else:
            assert result == 131
        world.settle(cell)
        assert cell.value == 127
    finally:
        hold.clear()


@pytest.mark.parametrize("constructor", [
    lambda cs: Cell("int", checksum=cs, source=Cell("int")),
    lambda cs: Cell("int", source=cs),
    lambda cs: Cell("int", source=Cell("text"), input_celltype="plain"),
], ids=["checksum-and-source", "bare-checksum-as-source", "conflicting-typed-declaration"])
def test_constructor_rejects_invalid_reference_declarations(world, constructor):
    checksum = Buffer(137, "int").get_checksum()
    with pytest.raises((TypeError, ValueError)):
        world.bind(constructor(checksum))


def test_with_input_returns_new_cell_without_mutating_original(world):
    first = world.make("int")
    first.set(139)
    source = world.make("int")
    source.set(149)
    derived = world.bind(first.with_input(source))
    world.settle(first, source, derived)
    assert first.value == 139
    assert derived.value == 149
    assert derived.source is not None


@pytest.mark.parametrize("attr", ["buffer", "value"])
def test_missing_result_buffer_raises_without_fingertip_or_failure(world, monkeypatch, attr):
    # A dummy result is deliberately not materialized or deposited anywhere.
    monkeypatch.setitem(sys.modules, "seamless_remote", None)
    missing = Checksum("de" * 32)
    cell = world.make("bytes", checksum=missing)
    world.settle(cell)
    def forbidden(*args, **kwargs):
        pytest.fail("a property read fingertipped its result")
    monkeypatch.setattr(Checksum, "fingertip", forbidden)
    assert cell.checksum == missing
    with pytest.raises(CacheMissError) as error:
        getattr(cell, attr)
    assert error.value.args == (missing,)
    assert cell.state == "complete"
    assert cell.checksum == missing
    assert cell.exception is None


@pytest.mark.parametrize("api", ["compute_async", "computation"])
def test_async_computation_returns_checksum(world, api):
    cell = world.make("int")
    cell.set(151)
    async def compute():
        return await getattr(cell, api)()
    assert asyncio.run(compute()) == Buffer(151, "int").get_checksum()
    assert cell.run() == 151


def test_build_aliases_snapshot_the_same_recipe(world):
    cell = world.make("plain")
    cell.set({"a": 157})
    world.settle(cell)
    selected = cell["a"]
    snapshots = [selected.build(), selected.expression(), selected()]
    assert all(isinstance(snapshot, Expression) for snapshot in snapshots)
    assert all(snapshot.identity_key == snapshots[0].identity_key for snapshot in snapshots)
    assert all(snapshot.run() == 157 for snapshot in snapshots)


@gap("deep one-step paths bypass ordinary project-or-convert validation")
@pytest.mark.parametrize("celltype,member_type,value", [
    ("deepcell", "mixed", {"answer": 163}),
    ("deepfolder", "bytes", b"deep folder member"),
    ("folder", "bytes", b"folder member"),
])
def test_deep_one_step_selects_member_checksum(world, celltype, member_type, value):
    member = Buffer(value, member_type)
    index = Buffer({"dir/file": member.get_checksum().hex()}, "plain")
    holds = [member.tempref(), index.tempref()]
    try:
        root = world.make(celltype, checksum=index.get_checksum())
        child = world.bind(root["dir/file"].as_celltype(member_type))
        world.settle(root, child)
        assert child.checksum == member.get_checksum()
        assert child.value == value
    finally:
        for hold in holds:
            hold.clear()


@pytest.mark.parametrize("celltype", celltypes + ["deepcell", "deepfolder", "folder", "module"])
def test_null_retype_keeps_checksum(world, celltype):
    cell = world.make(celltype)
    cell.set(None)
    world.settle(cell)
    checksum = Buffer(None, "plain").get_checksum()
    assert cell.checksum == checksum
    assert cell.value == (b"" if celltype == "bytes" else None)
    cell.celltype = "int"
    world.settle(cell)
    assert cell.checksum is not None
    assert cell.checksum == checksum
    assert cell.value is None
    assert cell.state == "complete"


@gap("checksum-preserving conversion + path must fuse with the converted input_celltype")
def test_preserving_conversion_then_path_fuses(world):
    root = world.make("plain")
    root.set({"a": 173})
    child = world.bind(root.as_celltype("mixed")["a"])
    world.settle(root, child)
    expected = Expression(root.checksum, input_celltype="mixed", celltype="mixed", path="a")
    actual = child.build()
    assert actual.compute() == expected.compute()
    assert actual.database_key == expected.database_key


@gap("reformatting conversion + path must retain the converted buffer as input")
def test_reformatting_conversion_then_path_does_not_fuse(world):
    root = world.make("str")
    root.set("word")
    child = world.bind(root.as_celltype("text")[0])
    world.settle(root, child)
    converted = Buffer("word", "text")
    hold = converted.tempref()
    try:
        actual = child.build()
        assert actual.run() == "w"
        expected = Expression(converted.get_checksum(), input_celltype="text", celltype="text", path="[0]")
        assert actual.database_key == expected.database_key
        assert actual.input_checksum != root.checksum
    finally:
        hold.clear()


@gap("two conversions never fuse; text -> plain -> mixed differs from text -> mixed")
def test_two_conversions_keep_the_intermediate_recipe(world):
    root = world.make("text")
    root.set("[1,2]")
    child = world.bind(root.as_celltype("plain").as_celltype("mixed"))
    world.settle(root, child)
    assert child.value == [1, 2]
    # text -> plain preserves JSON-parseable bytes (conversion contract).
    # Canonical serialization of [1, 2] need not have the same checksum.
    intermediate = Buffer(b"[1,2]\n")
    hold = intermediate.tempref()
    try:
        actual = child.build()
        actual.compute()
        expected = Expression(intermediate.get_checksum(), input_celltype="plain", celltype="mixed")
        assert actual.database_key == expected.database_key
    finally:
        hold.clear()


@gap("a deep step ends the fused run; child work is keyed by the member checksum")
def test_deep_step_is_a_fusion_barrier(world):
    member = Buffer({"a": 179}, "mixed")
    indexes = [Buffer({key: member.get_checksum().hex()}, "plain") for key in ("first", "second")]
    holds = [buffer.tempref() for buffer in [member, *indexes]]
    try:
        snapshots = []
        for key, index in zip(("first", "second"), indexes):
            root = world.make("deepcell", checksum=index.get_checksum())
            child = world.bind(root[key].as_celltype("mixed")["a"])
            world.settle(root, child)
            snapshot = child.build()
            assert snapshot.run() == 179
            snapshots.append(snapshot)
        expected = Expression(member.get_checksum(), input_celltype="mixed", celltype="mixed", path="a")
        assert snapshots[0].database_key == snapshots[1].database_key == expected.database_key
    finally:
        for hold in holds:
            hold.clear()


@gap("observed 2026-09-22: bound compute raises the deferred-validator failure instead of reporting it")
def test_deferred_validator_refuses_even_a_cached_recipe(world):
    buffer = Buffer(181, "int")
    hold = buffer.tempref()
    try:
        ordinary = world.make("int", checksum=buffer.get_checksum())
        world.settle(ordinary)
        assert ordinary.compute() == buffer.get_checksum()
        cell = world.make("int", checksum=buffer.get_checksum(), validator=Checksum("ab" * 32), validator_language="python")
        assert cell.build().identity_key == ordinary.build().identity_key
        assert cell.compute() is None
        assert cell.state == "failed"
        assert "validators are not implemented" in str(cell.exception)
    finally:
        hold.clear()
