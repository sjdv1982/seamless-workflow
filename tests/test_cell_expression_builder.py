"""Bound counterparts of the core Cell/Expression witness-corpus tests."""
from __future__ import annotations

import pytest

from seamless import Buffer, Cell, Expression
from seamless.checksum.expression import parse_path

from helpers.expression_hashtype_cases import (
    expression_case_id,
    iter_expression_cases,
)


EXPRESSION_CASES = tuple(iter_expression_cases())
VALID_EXPRESSION_CASES = tuple(
    (witness, case) for witness, case in EXPRESSION_CASES if case.valid
)
INVALID_EXPRESSION_CASES = tuple(
    (witness, case) for witness, case in EXPRESSION_CASES if not case.valid
)


def _parameters(cases):
    for witness, case in cases:
        if case.target_celltype != case.celltype:
            yield pytest.param(
                witness, case,
                marks=pytest.mark.xfail(
                    strict=False,
                    reason="cells.md Connecting: binding an Expression-backed conversion child is ahead of code",
                ),
            )
        else:
            yield witness, case


def _cell_from_case(ctx, source_checksum, case):
    ctx.source = Cell(checksum=source_checksum, celltype=case.celltype)
    cell = ctx.source
    for kind, payload in parse_path(case.path):
        if kind == "item":
            cell = cell[payload]
        elif kind == "slice":
            cell = cell[payload]
        else:
            raise AssertionError(kind)
    if case.target_celltype == case.celltype:
        # No conversion link is needed; projections remain bound.
        return cell
    ctx.result = cell.as_celltype(case.target_celltype)
    return ctx.result


@pytest.mark.parametrize(
    ("witness", "case"),
    _parameters(EXPRESSION_CASES),
    ids=[expression_case_id(witness, case) for witness, case in EXPRESSION_CASES],
)
def test_cell_built_expression_matches_direct_expression(make_context, witness, case):
    try:
        direct = case.build(witness.source_checksum)
    except ValueError as error:
        # Shape-invalid expressions are refused before evaluation (features 1–4).
        assert not case.valid
        with pytest.raises(type(error)):
            _cell_from_case(make_context(expression_execution="local"), witness.source_checksum, case).build()
        return
    built = _cell_from_case(make_context(expression_execution="local"), witness.source_checksum, case).build()

    assert built == direct
    assert built.identity_key == direct.identity_key
    assert built.database_key == direct.database_key


@pytest.mark.parametrize(
    ("witness", "case"),
    _parameters(VALID_EXPRESSION_CASES),
    ids=[expression_case_id(witness, case) for witness, case in VALID_EXPRESSION_CASES],
)
def test_cell_built_valid_expressions_match_direct_results(make_context, witness, case):
    input_buffer = Buffer(witness.raw_buffer, checksum=witness.source_checksum)
    direct = case.build(witness.source_checksum)
    built = _cell_from_case(make_context(expression_execution="local"), witness.source_checksum, case).build()

    assert input_buffer.checksum == witness.source_checksum
    assert built.compute() == direct.compute()
    assert _values_equal(built.run(), direct.run())


@pytest.mark.parametrize(
    ("witness", "case"),
    _parameters(INVALID_EXPRESSION_CASES),
    ids=[
        expression_case_id(witness, case)
        for witness, case in INVALID_EXPRESSION_CASES
    ],
)
def test_cell_built_invalid_expressions_match_direct_failures(make_context, witness, case):
    input_buffer = Buffer(witness.raw_buffer, checksum=witness.source_checksum)
    assert input_buffer.checksum == witness.source_checksum
    with pytest.raises(Exception) as direct_exc:
        case.build(witness.source_checksum).compute()
    with pytest.raises(Exception) as built_exc:
        _cell_from_case(make_context(expression_execution="local"), witness.source_checksum, case).build().compute()
    assert type(built_exc.value) is type(direct_exc.value)


def _values_equal(first, second) -> bool:
    try:
        import numpy as np

        if isinstance(first, np.ndarray) or isinstance(second, np.ndarray):
            return bool(np.array_equal(first, second))
    except ImportError:
        pass
    if isinstance(first, dict) and isinstance(second, dict):
        if first.keys() != second.keys():
            return False
        return all(_values_equal(first[key], second[key]) for key in first)
    if isinstance(first, (list, tuple)) and isinstance(second, (list, tuple)):
        if len(first) != len(second):
            return False
        return all(_values_equal(item1, item2) for item1, item2 in zip(first, second))
    return first == second
