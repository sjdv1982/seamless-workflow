"""Probe 2: distinguish conversion from reinterpretation (str -> text shows quotes)."""

from seamless import Buffer, Cell, Checksum


def typ(value):
    return f"{type(value).__name__}:{value!r}"


def report(label, fn):
    try:
        print(f"[{label}] ->", fn())
    except Exception as exc:
        print(f"[{label}] RAISES {type(exc).__name__}: {str(exc)[:240]}")


def pin_checksum(tf_obj, pin):
    t = tf_obj()
    t.construction_sync() if hasattr(t, "construction_sync") else None
    t.compute()
    d = t.transformation_checksum.resolve("plain")
    return d[pin]


def test_probe(make_context):
    int42 = Buffer(42, "int").get_checksum().hex()[:12]
    str42 = Buffer("42", "str").get_checksum().hex()[:12]
    print("int 42 checksum", int42, "| str '42' checksum", str42)

    # A. connected str Cell "hello" -> text pin
    ctx = make_context()
    ctx.src = Cell("str")
    ctx.src.set("hello")
    ctx.tf = typ
    ctx.tf.celltypes.value = "text"
    ctx.tf.celltypes.result = "str"
    ctx.tf.pins.value = ctx.src
    report("A reactive, str cell 'hello' -> text pin", lambda: (ctx.compute(timeout=10), ctx.tf.state, ctx.tf.result.value, ctx.tf.exception)[1:])
    report("A snapshot call", lambda: ctx.tf().run())

    # A2. connected str Cell "42" -> int pin: which checksum enters the transformation?
    ctx2 = make_context()
    ctx2.src = Cell("str")
    ctx2.src.set("42")
    ctx2.tf = typ
    ctx2.tf.celltypes.value = "int"
    ctx2.tf.celltypes.result = "str"
    ctx2.tf.pins.value = ctx2.src
    ctx2.compute(timeout=10)
    report("A2 tf input checksum for int pin fed by str cell", lambda: pin_checksum(ctx2.tf, "value"))

    # B. constant 'hello' in str pin, then celltypes.value = 'text'
    ctx3 = make_context()
    ctx3.tf = typ
    ctx3.tf.celltypes.value = "str"
    ctx3.tf.celltypes.result = "str"
    ctx3.tf.pins.value = "hello"
    ctx3.compute(timeout=10)
    report("B before", lambda: ctx3.tf.result.value)
    ctx3.tf.celltypes.value = "text"
    report("B reactive after retype str->text", lambda: (ctx3.compute(timeout=10), ctx3.tf.state, ctx3.tf.result.value, ctx3.tf.exception)[1:])
    report("B snapshot call after retype", lambda: ctx3.tf().run())
    report("B pins.value read after retype", lambda: repr(ctx3.tf.pins.value.value))

    # C. constant 'abc' in str pin, then retype to int (unconvertible)
    ctx4 = make_context()
    ctx4.tf = typ
    ctx4.tf.celltypes.value = "str"
    ctx4.tf.celltypes.result = "str"
    ctx4.tf.pins.value = "abc"
    ctx4.compute(timeout=10)
    ctx4.tf.celltypes.value = "int"
    report("C reactive after retype str 'abc'->int", lambda: (ctx4.compute(timeout=10), ctx4.tf.state, ctx4.tf.block_reason, str(ctx4.tf.pins.value.exception)[:200])[1:])
    # Cell parity for C
    ctx4.c = Cell("str")
    ctx4.c.set("abc")
    ctx4.compute(timeout=10)
    ctx4.c.celltype = "int"
    report("C' cell retype str 'abc'->int", lambda: (ctx4.compute(timeout=10), ctx4.c.state, str(ctx4.c.exception)[:200])[1:])

    # D. standalone substrate: upstream Transformation result str -> downstream int/text pin
    from seamless_transformer import delayed
    up = delayed(lambda: "hello")
    up.celltypes.result = "str"
    down = delayed(typ)
    down.celltypes.value = "text"
    down.celltypes.result = "str"
    report("D standalone: str-result Transformation -> text pin", lambda: down(value=up()).run())
    s = Cell("str")
    s.set("hello")
    report("D2 standalone: Cell('str').build() -> text pin", lambda: down(value=s.build()).run())
