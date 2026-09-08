"""Immutable, leased payloads and operations that must run outside graph turns."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from threading import Event, Thread
from seamless import Buffer, Checksum
from seamless.reference_lifecycle import register_refholder, safe_release_refholder


class Lease:
    def __init__(self, checksum, celltype='mixed'):
        self.checksum = checksum
        self.celltype = celltype
        self.released = False
        if checksum is not None:
            checksum.incref_refholder()
        register_refholder(self)

    def _refheld_checksums(self):
        return () if self.released or self.checksum is None else ((self.checksum, 'snapshot'),)

    def _release_refholds(self):
        if self.released:
            return
        self.released = True
        if self.checksum is not None:
            self.checksum.decref_refholder()

    def __del__(self):
        safe_release_refholder(self)


@dataclass(frozen=True)
class PreparedTransformer:
    config: object
    snapshot: object


@dataclass(frozen=True)
class PreparedCell:
    celltype: str
    target_celltype: str
    validator: object
    validator_language: object
    input_ref: object

    def _release_refholds(self):
        pass


class SideLoop:
    """Own loop, separate from both callers and controller. Never reads graph state."""
    def __init__(self, name):
        self.ready = Event()
        self.thread = Thread(target=self._run, name=name, daemon=True)
        self.thread.start()
        self.ready.wait()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()


def evaluate_cell(root, root_type, inputs, target_type):
    from seamless.checksum.expression import evaluate_expression
    from .context import _assign_path
    from .adapters import checksum_for_value
    value = root.resolve(root_type) if root is not None else {}
    for local, checksum, source_type in inputs:
        if isinstance(local[0], int) and not isinstance(value, (list, tuple)):
            raise TypeError("Integer Cell connection targets require an existing sequence")
        _assign_path(value, local, checksum.resolve(source_type))
    return checksum_for_value(value, target_type)


def evaluate_projection(checksum, path, celltype, target_celltype, validator=None, validator_language=None):
    from seamless.checksum.expression import evaluate_expression
    from .builder_state import _path_string
    result = evaluate_expression(checksum, _path_string(path), celltype, target_celltype,
                                 validator=validator, validator_language=validator_language)
    if result is None:
        raise KeyError(f'Expression path {path!r} does not exist')
    return result
