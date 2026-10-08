from seamless_transformer import delayed
from seamless_transformer.transformation_class import Transformation


def add(a, b):
    return a + b


def test_bound_transformer_streaming_reaches_fired_transformations(make_context, monkeypatch):
    fired = []
    computation = Transformation.computation

    async def record(self, *args, **kwargs):
        fired.append(self.streaming)
        return await computation(self, *args, **kwargs)

    monkeypatch.setattr(Transformation, "computation", record)

    ctx = make_context()
    ctx.tf = add
    assert ctx.tf.streaming is False
    ctx.tf.streaming = True
    assert ctx.tf.streaming is True  # node state: a fresh view sees it
    ctx.tf.pins.a = 2
    ctx.tf.pins.b = 3
    assert ctx.tf.run() == 5
    assert fired == [True]
    assert ctx.tf.build().streaming is True

    ctx.tf.streaming = False  # not part of demand: no re-run
    ctx.compute(timeout=10)
    assert fired == [True]

    tf = delayed(add)
    tf.streaming = True
    ctx.tf2 = tf  # binding carries the flag
    assert ctx.tf2.streaming is True
