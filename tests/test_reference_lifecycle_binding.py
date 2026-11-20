from __future__ import annotations

import gc

import pytest

from seamless import Buffer, Cell
from seamless.caching.buffer_cache import get_buffer_cache
from seamless.reference_lifecycle import audit_reference_accounting
from seamless_workflow import Context


def identity(value):
    return value


def _count(checksum):
    return get_buffer_cache().reference_snapshot().get(checksum, (0, 0, False))[0]


def test_checksum_backed_cell_binding_adopts_before_builder_release():
    buffer = Buffer(17, "int")
    checksum = buffer.get_checksum()
    cell = Cell(checksum=checksum, celltype="int")
    ctx = Context()
    assert _count(checksum) == 1
    ctx.value = cell
    assert cell._refholds_released is True
    assert _count(checksum) == 2  # graph producer plus current result
    assert any(role == "cell:value:literal" for _, role in ctx._refheld_checksums())
    del ctx
    del cell
    del buffer
    gc.collect()
    assert _count(checksum) == 0


@pytest.mark.parametrize("chain", ["projection", "project_then_convert", "convert_then_project"])
def test_binding_standalone_projection_chains_preserves_path_and_claims(chain, caplog):
    buffer = Buffer({"a": [1, 2]}, "mixed")
    checksum = buffer.get_checksum()
    source = Cell(checksum=checksum, celltype="mixed")
    if chain == "projection":
        cell = source["a"]
    elif chain == "project_then_convert":
        cell = source["a"].as_celltype("plain")
    else:
        cell = source.as_celltype("plain")["a"]

    ctx = Context()
    ctx.result = cell
    ctx.compute(timeout=10)
    assert ctx.result.value == [1, 2]
    assert cell._refholds_released is True

    del cell, source, buffer
    gc.collect()
    assert ctx.result.value == [1, 2]
    with caplog.at_level("WARNING", logger="seamless.references"):
        audit_reference_accounting()
    assert "live claims" not in caplog.text

    del ctx
    gc.collect()
    assert _count(checksum) == 0


def test_checksum_backed_transformer_binding_releases_standalone_builder():
    from seamless.transformer import delayed

    buffer = Buffer(19, "int")
    checksum = buffer.get_checksum()
    builder = delayed(identity)
    builder.args.value = checksum
    standalone_count = _count(checksum)
    ctx = Context()
    ctx.transformer = builder
    node = ctx._graph.nodes[("transformer",)]
    assert builder._refholds_released is True
    ctx.compute(timeout=10)
    assert _count(checksum) == standalone_count + 1
    assert any(
        role == "transformer:transformer:pin:value"
        for _, role in ctx._refheld_checksums()
    )
    assert any(
        role == "transformer:transformer:code"
        for _, role in ctx._refheld_checksums()
    )
    assert node.transformer_pin_producers["value"].checksum == checksum


def test_failed_builder_binding_rolls_back_and_keeps_builder_standalone():
    from seamless_workflow.errors import DependencyError

    other = Context()
    other.source = 1
    # Fails inside _create_cell_from_builder, after the node was created.
    cell = Cell(source=other.source)
    ctx = Context()
    with pytest.raises(DependencyError):
        ctx.value = cell
    assert ("value",) not in ctx._graph.nodes
    assert cell._workflow_backend is None
    assert cell._refholds_released is False
    cell._release_refholds()


def test_namespace_deletion_removes_descendants_and_roles():
    ctx = Context()
    ctx.sub = Context()
    ctx.sub.value = {"token": "namespace-delete"}
    checksum = ctx._graph.nodes[("sub", "value")].cell_root_producer.checksum
    assert _count(checksum) == 2
    del ctx.sub
    assert not any(path[:1] == ("sub",) for path in ctx._graph.nodes)
    assert ("sub",) not in ctx._graph.namespaces
    assert _count(checksum) == 0


def test_context_code_claim_is_derived_from_graph_state(caplog):
    ctx = Context()
    ctx.transformer = identity
    path = ("transformer",)
    checksum = ctx._graph.nodes[path].transformer_config.code_checksum
    checksum.decref_refholder()  # deliberate omitted acquisition
    with caplog.at_level("WARNING", logger="seamless.references"):
        audit_reference_accounting(holders=[ctx])
    assert "live claims" in caplog.text
    checksum.incref_refholder()
    ctx._release_refholds()


def test_checksum_module_replacement_releases_old_role():
    from seamless.transformer import delayed

    ctx = Context()
    ctx.transformer = delayed(identity)
    ctx.transformer.args.value = 1
    ctx.transformer.modules.example = {"__code__": "def helper(): return 1"}
    ctx.compute(timeout=10)
    path = ("transformer",)
    role = (path, "example")
    first = ctx._module_refholds[role][0]
    assert _count(first) == 1

    # The old claim remains until the replacement Transformation has supplied
    # its canonical module pin checksum.
    ctx.transformer.modules.example = {"__code__": "def helper(): return 2"}
    ctx.compute(timeout=10)
    second = ctx._module_refholds[role][0]
    assert second != first
    assert _count(first) == 0
    assert _count(second) == 1
    assert (second, "transformer:transformer:module:example") in tuple(
        ctx._refheld_checksums()
    )

    ctx.close(timeout=10)
    assert _count(second) == 0


@pytest.mark.parametrize("failing_step", ["pin", "code"])
def test_failed_transformer_staging_preserves_graph_and_roles(monkeypatch, caplog, failing_step):
    """A replacement that fails before publication is a rejected request (§12.3).

    Staging acquires the pin producer, then the code checksum, then module
    checksums.  Failing at each step checks that whatever was staged before it
    is released, that the live node is untouched, and that the Context stays
    usable.
    """
    import seamless_workflow.context as context_module
    from seamless.transformer import delayed

    ctx = Context()
    ctx.transformer = identity
    ctx.transformer.pins.value = 31
    ctx.compute(timeout=10)
    path = ("transformer",)
    node = ctx._graph.nodes[path]
    old_config = node.transformer_config
    old_producer = node.transformer_pin_producers["value"]
    old_claims = tuple(ctx._refheld_checksums())

    replacement_checksum = Buffer(32, "int").get_checksum()
    module_checksum = Buffer(b"staging-failure-module").get_checksum()
    replacement = delayed(identity)
    replacement.args.value = replacement_checksum
    replacement.modules.example = module_checksum
    staged = {
        "pin": replacement_checksum,
        "code": old_config.code_checksum,  # same code: staged as a second hold
        "module": module_checksum,
    }
    counts = {step: _count(checksum) for step, checksum in staged.items()}

    normalize_checksum = context_module.normalize_checksum

    def fail_at_step(checksum):
        if checksum == staged[failing_step]:
            raise RuntimeError("forced replacement staging failure")
        return normalize_checksum(checksum)

    with monkeypatch.context() as patch:
        patch.setattr(context_module, "normalize_checksum", fail_at_step)
        with pytest.raises(RuntimeError, match="forced replacement staging failure"):
            ctx.transformer = replacement

    node = ctx._graph.nodes[path]
    assert node.transformer_config is old_config
    assert node.transformer_pin_producers["value"] is old_producer
    assert tuple(ctx._refheld_checksums()) == old_claims
    assert {step: _count(checksum) for step, checksum in staged.items()} == counts
    assert replacement._workflow_backend is None
    assert replacement._refholds_released is False

    with caplog.at_level("WARNING", logger="seamless.references"):
        audit_reference_accounting(holders=[ctx, replacement])
    assert caplog.text == ""

    # A rejected request neither poisons the Context nor fails the live node.
    assert ctx.transformer.state == "complete"
    assert ctx.transformer.result.value == 31

    replacement._release_refholds()
    ctx._release_refholds()


def test_failure_after_transformer_publication_poisons_the_context(monkeypatch, caplog):
    """An internal failure after publication must poison or stop the Context (§12.3).

    Publication is the commit point and MOD-5 forbids rolling it back, so a
    Context that stays live after this failure reports the old node's result
    under the new configuration.
    """
    from seamless.transformer import delayed
    from seamless_workflow.errors import ClosedContextError, ControllerFailedError

    ctx = Context()
    ctx.transformer = identity
    ctx.transformer.pins.value = 31
    ctx.compute(timeout=10)
    handle = ctx.transformer

    replacement_buffer = Buffer(32, "int")  # keeps the new pin resolvable
    replacement = delayed(identity)
    replacement.args.value = replacement_buffer.get_checksum()

    def fail_after_publication():
        raise RuntimeError("forced failure after publication")

    with monkeypatch.context() as patch:
        patch.setattr(ctx, "_derive_all", fail_after_publication)
        with pytest.raises(RuntimeError, match="forced failure after publication"):
            ctx.transformer = replacement

    operations = {
        "state": lambda: ctx.transformer.state,
        "result": lambda: ctx.transformer.result.value,
        "held handle": lambda: handle.result.value,
        "barrier": lambda: ctx.compute(timeout=10),
        "write": lambda: setattr(ctx, "other", 1),
    }
    still_live = {}
    for name, operation in operations.items():
        try:
            still_live[name] = operation()
        except (ControllerFailedError, ClosedContextError):
            pass
    assert still_live == {}, f"the Context stayed live after an internal failure: {still_live}"

    ctx.close()
    assert ctx._refheld_checksums() == ()
    with caplog.at_level("WARNING", logger="seamless.references"):
        audit_reference_accounting(holders=[ctx, replacement])
    # This test poisons the controller on purpose: that is logged at ERROR by
    # seamless_workflow.controller, and must be the ONLY log output.
    poison_logs = [r for r in caplog.records if r.name == "seamless_workflow.controller"]
    assert poison_logs and all(r.levelname == "ERROR" and "poisoned" in r.getMessage() for r in poison_logs)
    assert [r for r in caplog.records if r.name != "seamless_workflow.controller"] == []
    replacement._release_refholds()


def test_transformer_replacement_transfers_pin_and_module_roles():
    from seamless.transformer import delayed

    ctx = Context()
    ctx.transformer = identity
    old_pin = Buffer(41, "int").get_checksum()
    ctx.transformer.pins.value = old_pin
    ctx.transformer.modules.example = {"__code__": "def helper(): return 1"}
    ctx.compute(timeout=10)
    old_module = ctx._module_refholds[(("transformer",), "example")][0]

    new_pin = Buffer(42, "int").get_checksum()
    replacement = delayed(identity)
    replacement.args.value = new_pin
    replacement.modules.example = {"__code__": "def helper(): return 2"}

    ctx.transformer = replacement
    ctx.compute(timeout=10)
    assert _count(old_pin) == 0
    assert _count(old_module) == 0
    assert _count(new_pin) == 1
    new_module = ctx._module_refholds[(("transformer",), "example")][0]
    assert _count(new_module) == 1
    assert replacement._refholds_released is True
    assert replacement._workflow_backend is not None

    ctx._release_refholds()
    assert _count(new_pin) == 0
    assert _count(new_module) == 0

def test_inline_module_refhold_adopts_merged_builder_pin_and_reuses_token():
    import copy

    from seamless import Checksum
    from seamless.transformer import delayed
    from seamless_transformer.transformer_class import PythonTransformer

    module = {"__code__": "def helper(): return 1"}
    builder = PythonTransformer("def identity(value): return value")
    builder.args.value = 1
    builder.modules.main = module
    builder.globals.answer = 42
    transformation = builder.build()
    transformation.construct()
    expected = Checksum(transformation._definition_payload_template["main"][2])
    transformation._pretransformation.release()
    transformation._release_refholds()

    ctx = Context()
    ctx.transformer = delayed("def identity(value): return value")
    ctx.transformer.args.value = 1
    ctx.transformer.modules.main = module
    ctx.transformer.globals.answer = 42
    ctx.compute(timeout=10)
    role = (("transformer",), "main")
    token = ctx._module_refhold_tokens[("transformer",)]
    assert ctx._module_refholds[role] == (expected, False)
    assert _count(expected) >= 1
    assert (expected, "transformer:transformer:module:main") in tuple(
        ctx._refheld_checksums()
    )

    # A changed input does not drop an unchanged module-config claim.
    ctx.transformer.args.value = 2
    ctx.compute(timeout=10)
    ctx.transformer.modules.main = copy.deepcopy(module)
    assert ctx._module_refhold_tokens[("transformer",)] == token
    assert ctx._module_refholds[role] == (expected, False)
    assert len([
        item for item in ctx._refheld_checksums()
        if item == (expected, "transformer:transformer:module:main")
    ]) == 1
    ctx.close(timeout=10)
    assert _count(expected) == 0



def test_module_type_pin_is_adopted_from_built_transformation():
    import types

    from seamless.transformer import delayed

    module = types.ModuleType("inline_helper")
    module.answer = 42
    ctx = Context()
    ctx.transformer = delayed("def identity(value): return value")
    ctx.transformer.args.value = 1
    ctx.transformer.modules.helper = module
    ctx.compute(timeout=10)

    checksum = ctx._module_refholds[(("transformer",), "helper")][0]
    assert checksum in [value for value, _role in ctx._refheld_checksums()]
    assert checksum.resolve("plain")
    ctx.close(timeout=10)
    assert _count(checksum) == 0


def test_set_graph_transfers_overlapping_inline_module_roles_and_close_releases_once():
    import copy

    from seamless.transformer import delayed

    ctx = Context()
    ctx.first = delayed("def identity(value): return value")
    ctx.second = delayed("def identity(value): return value")
    module = {"__code__": "def helper(): return 1"}
    ctx.first.args.value = 1
    ctx.second.args.value = 1
    ctx.first.modules.helper = module
    ctx.second.modules.helper = module
    ctx.compute(timeout=10)
    checksum = ctx._module_refholds[(("first",), "helper")][0]

    def module_roles():
        return [
            role for candidate, role in ctx._refheld_checksums()
            if candidate == checksum and ":module:helper" in role
        ]

    assert len(module_roles()) == 2
    assert len(ctx._module_refholds) == 2
    assert _count(checksum) >= 2

    existing_module_refholds = dict(ctx._module_refholds)
    ctx.set_graph(copy.deepcopy(ctx.get_graph()))
    assert ctx._module_refholds == existing_module_refholds
    ctx.compute(timeout=10)
    assert len(module_roles()) == 2
    assert len(ctx._module_refholds) == 2
    assert _count(checksum) >= 2

    del ctx.first
    ctx.compute(timeout=10)
    assert len(module_roles()) == 1
    assert set(ctx._module_refholds) == {(("second",), "helper")}
    assert _count(checksum) >= 1

    ctx.close(timeout=10)
    assert _count(checksum) == 0
    assert ctx._module_refholds == {}
    assert ctx._module_refhold_tokens == {}


def test_checksum_main_with_globals_keeps_builder_error_timing():
    from seamless import Buffer
    from seamless.transformer import delayed
    from seamless_transformer.transformer_class import PythonTransformer

    ctx = Context()
    ctx.transformer = delayed("def identity(value): return value")
    ctx.transformer.args.value = 1
    module_checksum = Buffer(b"checksum-main-with-globals").get_checksum()
    ctx.transformer.modules.main = module_checksum
    ctx.transformer.globals.answer = 42

    # Assignment itself stays valid; the builder diagnoses the combination.
    assert not ctx._module_refholds
    builder = PythonTransformer("def identity(value): return value")
    builder.args.value = 1
    builder.modules.main = module_checksum
    builder.globals.answer = 42
    with pytest.raises(AttributeError):
        builder.build()
    builder._release_refholds()

    ctx.compute(timeout=10)
    assert ctx._graph.nodes[("transformer",)].state == "failed"
    assert not ctx._module_refholds
    ctx.close(timeout=10)
    assert _count(module_checksum) == 0


def test_module_type_config_is_not_snapshotted_during_config_sync():
    import types

    from seamless.transformer import delayed

    ctx = Context()
    ctx.transformer = delayed("def identity(value): return value")
    module = types.ModuleType("mutable_module")
    module.answer = 41
    ctx.transformer.modules.example = module
    stored = ctx._graph.nodes[("transformer",)].transformer_config.modules["example"]
    module.answer = 42
    assert stored is module

    ctx.transformer.args.value = 1
    ctx.compute(timeout=10)
    checksum = ctx._module_refholds[(("transformer",), "example")][0]
    assert checksum.resolve("plain")
    ctx.close(timeout=10)

def test_inline_module_bytes_survive_natural_expiry_until_context_close():
    import os
    import time

    from seamless import CacheMissError
    from seamless.transformer import delayed

    scale = os.environ.get("SEAMLESS_DEBUG_TEMPREF_SCALE")
    if not scale or float(scale) > 0.01:
        pytest.skip("run with SEAMLESS_DEBUG_TEMPREF_SCALE=0.01 for accelerated eviction")

    ctx = Context()
    ctx.transformer = delayed("def identity(value): return value")
    ctx.transformer.args.value = 1
    ctx.transformer.modules.example = {"__code__": "def hold_me(): return 9"}
    ctx.compute(timeout=10)
    checksum = ctx._module_refholds[(("transformer",), "example")][0]

    time.sleep(3.2)
    assert checksum.resolve("plain")["__code__"].startswith("def hold_me")

    ctx.close(timeout=10)
    with pytest.raises(CacheMissError):
        checksum.resolve()

def test_stale_built_module_adoption_is_ignored_after_config_replacement():
    from seamless.transformer import delayed
    from seamless_workflow.sidework import Lease

    ctx = Context()
    ctx.transformer = delayed("def identity(value): return value")
    ctx.transformer.args.value = 1
    ctx.transformer.modules.example = {"__code__": "def old_helper(): return 1"}
    ctx.compute(timeout=10)
    path = ("transformer",)
    role = (path, "example")
    old_checksum = ctx._module_refholds[role][0]
    old_record = ctx._runtime.current_runs[path]
    old_generation = old_record.generation
    old_token = ctx._module_refhold_tokens[path]

    ctx.transformer.modules.example = {"__code__": "def new_helper(): return 2"}
    ctx.compute(timeout=10)
    new_checksum = ctx._module_refholds[role][0]
    assert new_checksum != old_checksum

    stale_lease = Lease(old_checksum, role="example")
    ctx._adopt_built_module_refholds(
        path, old_generation, old_token, stale_lease
    )
    assert stale_lease.released
    assert ctx._module_refholds[role][0] == new_checksum
    assert ctx._module_refhold_tokens[path] == ctx._graph.nodes[path].transformer_config.config_token

    ctx.close(timeout=10)
