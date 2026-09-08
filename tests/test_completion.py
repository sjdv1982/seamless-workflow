"""Dynamic containers expose their current members without evaluating nodes."""
import pytest

from seamless_transformer import delayed


def add(alpha, beta=1):
    return alpha + beta


def test_context_and_nested_namespace_completion(make_context):
    ctx = make_context()
    ctx.scalar = 1
    ctx.group.leaf = 2
    ctx.group.deeper.tip = 3
    ctx.empty = make_context()
    assert {"scalar", "group", "empty", "compute"} <= set(dir(ctx))
    assert "leaf" not in dir(ctx)
    assert {"leaf", "deeper"} <= set(dir(ctx.group))
    assert "tip" not in dir(ctx.group)
    assert "tip" in dir(ctx.group.deeper)
    assert isinstance(dir(ctx.empty), list)
    view = ctx.group
    del ctx.group.leaf
    ctx.group.new_leaf = 4
    assert "leaf" not in dir(view)
    assert "new_leaf" in dir(view)


def test_missing_view_completion_tracks_later_assignments(make_context):
    ctx = make_context()
    missing = ctx.future
    before = ctx.get_graph()
    assert "child" not in dir(missing)
    assert ctx.get_graph() == before
    ctx.future.child = 1
    assert "child" in dir(missing)
    del ctx.future
    assert "child" not in dir(missing)


@pytest.mark.parametrize("bound", [False, True])
def test_pin_completion_includes_unassigned_and_optional_pins(make_context, bound):
    tf = delayed(add)
    if bound:
        ctx = make_context()
        ctx.tf = tf
        tf = ctx.tf
    assert {"alpha", "beta"} <= set(dir(tf.pins))
    assert {"alpha", "beta"} <= set(dir(tf.args))
    assert "result" not in dir(tf.pins)
    assert "__getitem__" in dir(tf.pins)
    assert {"pins", "result", "compute"} <= set(dir(tf))


@pytest.mark.parametrize("bound", [False, True])
def test_mapping_completion_tracks_changes(make_context, bound):
    tf = delayed(add)
    if bound:
        ctx = make_context()
        ctx.tf = tf
        tf = ctx.tf
    assert {"alpha", "beta", "result"} <= set(dir(tf.celltypes))
    modules = tf.modules
    tf.modules.math = {"type": "interpreted", "language": "python", "code": "value = 1"}
    assert "math" in dir(modules)
    del tf.modules.math
    assert "math" not in dir(modules)
    globals_view = tf.globals
    tf.globals.offset = 10
    assert "offset" in dir(globals_view)
    del tf.globals.offset
    assert "offset" not in dir(globals_view)


def test_ipython_attribute_completion(make_context):
    completer_module = pytest.importorskip("IPython.core.completer")
    ctx = make_context()
    ctx.branch.leaf = 1
    ctx.tf = add
    completer = completer_module.IPCompleter(
        namespace={"ctx": ctx}, use_jedi=False, evaluation="unsafe"
    )
    assert "ctx.branch" in completer.attr_matches("ctx.br")
    assert "ctx.branch.leaf" in completer.attr_matches("ctx.branch.le")
    assert "ctx.tf.pins.alpha" in completer.attr_matches("ctx.tf.pins.al")
