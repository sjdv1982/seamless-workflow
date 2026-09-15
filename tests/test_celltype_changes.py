"""Changing a populated cell's type must convert its existing content.

Exercise both literal/checksum authority and live upstream dependencies. These
are contract tests, deliberately not xfailed when conversion is broken.
"""

import pytest

from seamless import Buffer, Cell


def identity(value):
    return value


@pytest.mark.parametrize("source_kind", ["literal", "checksum", "cell", "transformer"])
@pytest.mark.parametrize(
    "old_type,new_type,value,expected,next_value,next_expected,keeps_checksum",
    [
        pytest.param("int", "int", 42, 42, 53, 53, True, id="same-type"),
        pytest.param("int", "float", 42, 42.0, 53, 53.0, True, id="int-to-float"),
        pytest.param("str", "int", "42", 42, "53", 53, True, id="str-to-int"),
        pytest.param("str", "text", "hello", "hello", "world", "world", False, id="str-to-text"),
        pytest.param("text", "str", "hello", "hello", "world", "world", False, id="text-to-str"),
    ],
)
def test_populated_celltype_change(
    make_context, source_kind, old_type, new_type, value, expected,
    next_value, next_expected, keeps_checksum,
):
    ctx = make_context()
    if source_kind == "checksum":
        buffer = Buffer(value, old_type)
        ctx.target = Cell(old_type, checksum=buffer.get_checksum())
    else:
        ctx.target = Cell(old_type)
        if source_kind == "literal":
            ctx.target.set(value)
        else:
            ctx.source = Cell(old_type)
            ctx.source.set(value)
            if source_kind == "cell":
                ctx.target = ctx.source
            else:
                ctx.identity = identity
                ctx.identity.celltypes.value = old_type
                ctx.identity.celltypes.result = old_type
                ctx.identity.pins.value = ctx.source
                ctx.target = ctx.identity
    ctx.downstream = ctx.target
    ctx.compute(timeout=10)
    original_checksum = ctx.target.checksum
    assert original_checksum is not None
    assert original_checksum == Buffer(value, old_type).get_checksum()
    assert ctx.target.state == "complete"
    assert ctx.target.value == value
    connections = ctx.get_graph()["connections"]

    ctx.target.celltype = new_type
    ctx.compute(timeout=10)

    assert ctx.target.celltype == new_type
    assert ctx.target.state == "complete", ctx.target.exception
    assert ctx.target.checksum is not None
    # A conversion between nested celltypes keeps the checksum; a reformat makes a new one.
    if keeps_checksum:
        assert ctx.target.checksum == original_checksum
    else:
        assert ctx.target.checksum == Buffer(expected, new_type).get_checksum()
    assert ctx.target.value == expected
    assert type(ctx.target.value) is type(expected)
    assert ctx.downstream.state == "complete", ctx.downstream.exception
    assert ctx.downstream.value == value
    assert type(ctx.downstream.value) is type(value)
    assert ctx.downstream.checksum == original_checksum
    assert ctx.get_graph()["connections"] == connections

    if source_kind in {"cell", "transformer"}:
        # Retyping must not turn a computed cell into a captured literal.
        assert ctx.source.celltype == old_type
        assert ctx.source.checksum == original_checksum
        ctx.source.set(next_value)
        stored = Buffer(next_value, old_type).get_checksum()
    else:
        # A literal written after the retype is serialized as the new celltype.
        ctx.target.set(next_expected)
        stored = Buffer(next_expected, new_type).get_checksum()
    ctx.compute(timeout=10)
    assert ctx.target.state == "complete", ctx.target.exception
    if keeps_checksum:
        assert ctx.target.checksum == stored
        downstream_checksum = stored
    else:
        assert ctx.target.checksum == Buffer(next_expected, new_type).get_checksum()
        downstream_checksum = Buffer(next_value, old_type).get_checksum()
    assert ctx.target.value == next_expected
    assert ctx.downstream.value == next_value
    assert ctx.downstream.checksum == downstream_checksum

    # A second type change also has to use the correct source serialization.
    ctx.target.celltype = old_type
    ctx.compute(timeout=10)
    assert ctx.target.state == "complete", ctx.target.exception
    assert ctx.target.checksum == downstream_checksum
    assert ctx.target.value == next_value
    assert ctx.downstream.value == next_value
    assert ctx.downstream.checksum == downstream_checksum
