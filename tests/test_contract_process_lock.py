"""Bound lock metadata, graph persistence and configuration-token behavior."""

import json

import pytest

from seamless_transformer import delayed
from seamless_transformer.transformation_utils import resolve_lock_meta


def _identity(value: int):
    return value


def _node(context):
    return next(node for node in context.get_graph()["nodes"] if node["path"] == ["tf"])


def _token(context):
    return context.tf._workflow_backend.cfg.config_token


def _pair(builder):
    return builder.process_lock, builder.driver


@pytest.mark.parametrize("route", ["property", "meta"])
def test_bound_lock_coupling_matches_standalone_and_updates_node_metadata(make_context, route):
    context = make_context()
    context.tf = delayed(_identity)
    standalone = delayed(_identity)
    assert _pair(context.tf) == _pair(standalone) == (True, False)
    operations = [
        ("driver", True, (False, True)),
        ("driver", False, (False, False)),
        ("process_lock", True, (True, False)),
        ("process_lock", False, (False, False)),
        ("driver", True, (False, True)),
        ("driver", None, (False, False)),
    ]
    for key, value, expected in operations:
        for builder in (standalone, context.tf):
            if route == "property":
                setattr(builder, key, value)
            else:
                builder.meta = {key: value}
        assert _pair(context.tf) == _pair(standalone) == expected
        assert resolve_lock_meta(_node(context).get("meta", {})) == expected
        assert resolve_lock_meta(context.tf.build(value=17).meta) == expected
        assert resolve_lock_meta(standalone.build(value=17).meta) == expected
    assert _node(context)["meta"]["process_lock"] is False


@pytest.mark.parametrize("route", ["property", "meta"])
def test_bound_refuses_lock_reenable_on_driver_without_any_mutation(make_context, route):
    context = make_context()
    context.tf = delayed(_identity)
    context.tf.driver = True
    before = _node(context)
    token = _token(context)
    with pytest.raises((TypeError, ValueError)):
        if route == "property":
            context.tf.process_lock = True
        else:
            context.tf.meta = {"process_lock": True, "ncores": 9}
    assert _node(context) == before
    assert _token(context) == token
    assert _pair(context.tf) == (False, True)
    context.tf.meta = {"driver": False, "process_lock": True}
    assert _pair(context.tf) == (True, False)


def test_bound_combined_meta_conflict_is_refused_atomically(make_context):
    context = make_context()
    context.tf = delayed(_identity)
    context.tf.meta = {"ncores": 2}
    before = _node(context)
    with pytest.raises((TypeError, ValueError)):
        context.tf.meta = {"driver": True, "process_lock": True, "ncores": 8}
    assert _node(context) == before
    assert context.tf.meta["ncores"] == 2


@pytest.mark.parametrize("route", ["property", "meta"])
@pytest.mark.parametrize("key", ["driver", "process_lock"])
@pytest.mark.parametrize("value", [1, 0, "true", [], {}])
def test_bound_rejects_nonboolean_lock_settings(make_context, route, key, value):
    context = make_context()
    context.tf = delayed(_identity)
    before = _node(context)
    with pytest.raises((TypeError, ValueError)):
        if route == "property":
            setattr(context.tf, key, value)
        else:
            context.tf.meta = {key: value}
    assert _node(context) == before


@pytest.mark.parametrize("settings,expected", [
    ({}, (True, False)),
    ({"process_lock": True}, (True, False)),
    ({"process_lock": False}, (False, False)),
    ({"driver": True}, (False, True)),
    ({"driver": False, "process_lock": True}, (True, False)),
    ({"driver": True, "process_lock": False}, (False, True)),
    ({"driver": False, "process_lock": False}, (False, False)),
])
def test_bound_and_standalone_build_snapshots_have_same_flags(make_context, settings, expected):
    context = make_context()
    context.tf = delayed(_identity)
    standalone = delayed(_identity)
    context.tf.meta = settings
    standalone.meta = settings
    bound_handle = context.tf.build(value=23)
    standalone_handle = standalone.build(value=23)
    assert resolve_lock_meta(bound_handle.meta) == resolve_lock_meta(standalone_handle.meta) == expected
    # Mutating the live node must not rewrite either detached snapshot.
    context.tf.meta = {"driver": False, "process_lock": not expected[0]}
    assert resolve_lock_meta(bound_handle.meta) == expected
    assert resolve_lock_meta(standalone_handle.meta) == expected


@pytest.mark.parametrize("settings,expected", [
    ({"process_lock": True, "driver": False}, (True, False)),
    ({"process_lock": False, "driver": False}, (False, False)),
    ({"process_lock": False, "driver": True}, (False, True)),
])
def test_graph_file_roundtrip_preserves_explicit_lock_metadata(make_context, tmp_path, settings, expected):
    context = make_context()
    context.tf = delayed(_identity)
    context.tf.meta = settings
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps(context.get_graph()))
    saved_node = next(node for node in json.loads(graph_file.read_text())["nodes"] if node["path"] == ["tf"])
    assert saved_node["meta"]["process_lock"] is expected[0]
    assert saved_node["meta"].get("driver", False) is expected[1]
    if settings["driver"] is False:
        assert "driver" not in saved_node["meta"], "cleared driver must stay absent in node metadata"
    else:
        assert saved_node["meta"]["driver"] is True
    restored = make_context()
    restored.set_graph(json.loads(graph_file.read_text()))
    assert _pair(restored.tf) == expected
    snapshot = restored.tf.build(value=29)
    assert snapshot.meta["process_lock"] is expected[0]
    assert snapshot.meta.get("driver", False) is expected[1]
    assert resolve_lock_meta(snapshot.meta) == expected
    restored.tf.driver = False
    assert restored.tf.process_lock is expected[0]


@pytest.mark.parametrize("legacy_meta,expected", [
    (None, (True, False)),
    ({}, (True, False)),
    ({"driver": False}, (True, False)),
    ({"driver": True}, (False, True)),
])
def test_legacy_graph_without_process_lock_uses_absent_rule(make_context, tmp_path, legacy_meta, expected):
    context = make_context()
    context.tf = delayed(_identity)
    graph = context.get_graph()
    node = next(node for node in graph["nodes"] if node["path"] == ["tf"])
    if legacy_meta is None:
        node.pop("meta", None)
    else:
        node["meta"] = legacy_meta
    graph_file = tmp_path / "legacy.json"
    graph_file.write_text(json.dumps(graph))
    restored = make_context()
    restored.set_graph(json.loads(graph_file.read_text()))
    assert _pair(restored.tf) == expected
    assert resolve_lock_meta(restored.tf.build(value=31).meta) == expected


def test_operational_flags_change_config_token_but_streaming_does_not(make_context):
    context = make_context()
    context.tf = delayed(_identity)
    initial = _token(context)
    context.tf.streaming = True
    assert _token(context) == initial
    context.tf.streaming = False
    assert _token(context) == initial
    context.tf.process_lock = False
    unlocked = _token(context)
    assert unlocked != initial
    context.tf.driver = True
    driver = _token(context)
    assert driver != unlocked
    context.tf.driver = False
    assert _token(context) != driver
    ordinary = _token(context)
    context.tf.scratch = True
    assert _token(context) != ordinary
    scratch = _token(context)
    context.tf.streaming = True
    assert _token(context) == scratch
