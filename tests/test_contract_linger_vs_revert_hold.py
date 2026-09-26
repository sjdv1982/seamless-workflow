"""Ruling 7 (contract-clarity-rulings.md): the Expression linger and the
node-state self-edit revert hold are TWO knobs, because they answer different
events (a second requester arriving for the same evaluation, versus a person
reverting an edit). Tuning one must never retune the other.

Neither value is contract here (the linger is an internal constant; the revert
hold's figure belongs to node-state-lifecycle.md), only their independence.
"""

from time import time

from seamless.checksum import expression as expression_mod
from seamless_workflow.scheduler import Scheduler


def test_moving_the_linger_leaves_the_revert_hold_alone(monkeypatch):
    default_hold = Scheduler().self_edit_hold_seconds
    monkeypatch.setattr(expression_mod, "_EXPRESSION_LINGER", 0.123)

    scheduler = Scheduler()
    assert scheduler.self_edit_hold_seconds == default_hold
    before = time()
    deadline = scheduler.hold_deadline("self-edit")
    assert deadline - before >= default_hold - 0.5


def test_moving_the_revert_hold_leaves_the_linger_alone():
    linger = expression_mod._EXPRESSION_LINGER
    scheduler = Scheduler(self_edit_hold_seconds=99.0)

    before = time()
    deadline = scheduler.hold_deadline("self-edit")
    assert 98.5 <= deadline - before <= 99.5
    assert expression_mod._EXPRESSION_LINGER == linger
