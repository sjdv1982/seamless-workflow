"""Port of the three original celltype probes, with parity assertions.

Run from seamless-workflow with the seamless1 Python environment.
"""
from seamless import Buffer, Cell, Expression
from seamless_workflow import Context


def check(cell, expected, celltype):
    assert cell.value == expected
    assert cell.run() == expected
    assert cell.build().run() == expected
    assert cell.checksum == Buffer(expected, celltype).get_checksum()


with Context() as ctx:
    # Retyping converts the stored input in both modes.
    standalone = Cell("int")
    standalone.set(5)
    ctx.retyped = standalone
    standalone.celltype = "float"
    ctx.retyped.celltype = "float"
    ctx.compute(timeout=10)
    for cell in (standalone, ctx.retyped):
        check(cell, 5.0, "float")
        assert cell.input_celltype == "int"

    # Typed source construction, followed by explicit output retyping.
    upstream = Cell("str")
    upstream.set("5")
    ctx.upstream = upstream
    ctx.connected = Cell("text", source=ctx.upstream)
    ctx.rewired = ctx.upstream
    ctx.rewired.celltype = "text"
    ctx.downstream = ctx.connected
    ctx.compute(timeout=10)
    for cell in (
        Cell("text", source=upstream),
        Cell("text", source=upstream.build()),
        ctx.connected, ctx.rewired, ctx.downstream,
    ):
        check(cell, "5", "text")

    # A declared checksum carries its original interpretation through binding.
    declared = Cell("str", checksum=Buffer(5, "int").get_checksum(),
                    input_celltype="int")
    ctx.declared = declared
    ctx.compute(timeout=10)
    check(declared, "5", "str")
    check(ctx.declared, "5", "str")

    first = Expression(Buffer(5, "int").get_checksum(),
                       input_celltype="int", celltype="text")
    second = Expression(first)
    assert second.input_celltype == second.celltype == "text"
    assert first.run() == second.run() == "5"

print("All three ported probe groups agree: values, builds, and checksums.")
