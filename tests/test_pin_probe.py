"""Probe: how Transformer pins treat celltypes (bound mode), vs Cell parity."""

import pytest
from seamless import Buffer, Cell


def ident(value):
    return value


def typ(value):
    return f"{type(value).__name__}:{value!r}"


def report(label, fn):
    try:
        print(f"[{label}] ->", fn())
    except Exception as exc:
        print(f"[{label}] RAISES {type(exc).__name__}: {str(exc)[:200]}")


def test_probe(make_context):
    # 1. Connected Cell (str) into an int pin: convert or reinterpret?
    ctx = make_context()
    ctx.src = Cell("str")
    ctx.src.set("42")
    ctx.tf = typ
    ctx.tf.celltypes.value = "int"
    ctx.tf.celltypes.result = "str"
    ctx.tf.pins.value = ctx.src
    report("1 reactive run, str cell -> int pin", lambda: (ctx.compute(timeout=10), ctx.tf.result.value, ctx.tf.exception)[1:])
    report("1 snapshot call  ctx.tf().run()", lambda: ctx.tf().run())

    # 1b. text cell into int pin (text '42' is not JSON)
    ctx2 = make_context()
    ctx2.src = Cell("text")
    ctx2.src.set("42")
    ctx2.tf = typ
    ctx2.tf.celltypes.value = "int"
    ctx2.tf.celltypes.result = "str"
    ctx2.tf.pins.value = ctx2.src
    report("1b reactive run, text cell -> int pin", lambda: (ctx2.compute(timeout=10), ctx2.tf.state, ctx2.tf.result.value, ctx2.tf.exception)[1:])

    # 2. Invalid constant for an int pin
    ctx3 = make_context()
    ctx3.tf = typ
    ctx3.tf.celltypes.value = "int"
    ctx3.tf.celltypes.result = "str"
    report("2 tf.pins.value = 'abc' (int pin)", lambda: setattr(ctx3.tf.pins, "value", "abc"))
    report("2 state after", lambda: (ctx3.tf.state, ctx3.tf.exception))
    # Cell parity
    ctx3.c = Cell("int")
    report("2' cell(int).set('abc')", lambda: ctx3.c.set("abc"))
    report("2' cell state after", lambda: (ctx3.c.state, ctx3.c.exception))

    # 3. Constant set, then pin celltype changed
    ctx4 = make_context()
    ctx4.tf = typ
    ctx4.tf.celltypes.value = "int"
    ctx4.tf.celltypes.result = "str"
    ctx4.tf.pins.value = 42
    ctx4.compute(timeout=10)
    report("3 before: int pin, 42", lambda: ctx4.tf.result.value)
    ctx4.tf.celltypes.value = "str"
    report("3 after celltypes.value='str': reactive", lambda: (ctx4.compute(timeout=10), ctx4.tf.state, ctx4.tf.result.value, ctx4.tf.exception)[1:])
    report("3 after: snapshot call ctx.tf().run()", lambda: ctx4.tf().run())
    report("3 after: tf.pins.value read", lambda: ctx4.tf.pins.value.value)
    ctx4.tf.celltypes.value = "float"
    report("3b after celltypes.value='float': reactive", lambda: (ctx4.compute(timeout=10), ctx4.tf.state, ctx4.tf.result.value, ctx4.tf.exception)[1:])

    # 4. Standalone parity: stored input checksums convert at call
    from seamless_transformer import delayed
    tf = delayed(typ)
    tf.celltypes.value = "int"
    tf.celltypes.result = "str"
    tf.pins.value = 42
    tf.celltypes.value = "str"
    report("4 standalone: int 42 then celltype str", lambda: tf().run())
    tf.pins.value = "abc"
    tf.celltypes.value = "int"
    report("4b standalone: 'abc' into int pin at call", lambda: tf().run())
    ctx5 = make_context()
    s = Cell("str"); s.set("42")
    tf2 = delayed(typ)
    tf2.celltypes.value = "int"
    tf2.celltypes.result = "str"
    report("4c standalone: str Cell into int pin", lambda: (setattr(tf2.pins, "value", s), tf2().run())[1])
