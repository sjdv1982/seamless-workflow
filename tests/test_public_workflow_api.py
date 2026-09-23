from seamless import Cell
from seamless.workflow import Context, Transformer
from seamless_transformer import Transformer as CoreTransformer


def test_workflow_namespace_exports_canonical_authoring_types():
    assert Transformer is CoreTransformer
    assert Context.__name__ == "Context"
    assert Cell.__name__ == "Cell"


def test_code_less_factory_builder_can_be_bound_then_configured():
    ctx = Context()
    ctx.tf = Transformer()
    assert type(ctx.tf).__name__ == "PythonTransformer"
    assert ctx.tf.language == "python"
    assert ctx.tf.state == "unwired"

    ctx.tf.code = lambda value: value + 1
    ctx.tf.pins.value = 2
    assert ctx.tf.run() == 3
    ctx.close()
