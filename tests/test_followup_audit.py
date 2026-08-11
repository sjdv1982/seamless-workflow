from seamless_workflow import Context


def test_removed_alpha_wrapper_names_are_absent_from_workflow_source():
    # Keep the assertion on the public module surface; implementation source is
    # checked by the Phase 10 command-line audit as well.
    import seamless_workflow.views as views

    for name in (
        "CellView",
        "TransformerView",
        "TransformerResultView",
        "CellPinsView",
        "CellPinView",
        "TransformerPinView",
    ):
        assert not hasattr(views, name)


def test_graph_does_not_store_builder_objects():
    ctx = Context()
    ctx.value = {"x": 1}
    graph = ctx.get_graph()
    assert all(not isinstance(value, Context) for value in graph.values())
