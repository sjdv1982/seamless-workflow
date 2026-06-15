"""Backends that bind public builders to workflow Context state."""

from __future__ import annotations

from typing import Any

from seamless import Checksum
from seamless.cell_class import _UNSET


class BoundCellBackend:
    def __init__(self, context, node_path: tuple[str, ...], path: tuple[str, ...] = ()) -> None:
        self.context = context
        self.node_path = node_path
        self.owner_path = path

    @property
    def input_ref(self):
        return None

    @input_ref.setter
    def input_ref(self, value):
        self.set(value)

    @property
    def path(self) -> str:
        return ".".join(self.owner_path)

    @path.setter
    def path(self, value):
        raise AttributeError("Bound workflow Cell path is controlled by the Context")

    @property
    def path_python(self) -> str:
        return self.path

    @property
    def celltype(self) -> str:
        return self.context._graph.nodes[self.node_path].cell_config.celltype

    @celltype.setter
    def celltype(self, value: str) -> None:
        self.context._set_cell_config(self.node_path, celltype=value)

    @property
    def target_celltype(self) -> str:
        return self.context._graph.nodes[self.node_path].cell_config.target_celltype

    @target_celltype.setter
    def target_celltype(self, value: str | None) -> None:
        self.context._set_cell_config(self.node_path, target_celltype=value)

    @property
    def validator(self) -> Any:
        return self.context._graph.nodes[self.node_path].cell_config.validator

    @validator.setter
    def validator(self, value: Any) -> None:
        self.context._set_cell_config(self.node_path, validator=value)

    @property
    def validator_language(self) -> str | None:
        return self.context._graph.nodes[self.node_path].cell_config.validator_language

    @validator_language.setter
    def validator_language(self, value: str | None) -> None:
        self.context._set_cell_config(self.node_path, validator_language=value)

    @property
    def pins(self):
        from .views import CellPinsView

        return CellPinsView(self.context, self.node_path)

    @property
    def checksum(self):
        return self.context._get_checksum(self.node_path, self.owner_path)

    @property
    def buffer(self):
        return self.context._get_buffer(self.node_path, self.owner_path)

    @property
    def value(self):
        return self.context._get_value(self.node_path, self.owner_path)

    def derive(self, **updates):
        from seamless import Cell

        path = updates.get("path", self.path)
        if isinstance(path, str):
            owner_path = tuple(part for part in path.split(".") if part)
        else:
            owner_path = tuple(path or ())
        cell = Cell()
        cell._workflow_backend = BoundCellBackend(self.context, self.node_path, owner_path)
        return cell

    def set(self, value: Any) -> None:
        self.context._set_cell_value(self.node_path, self.owner_path, value)

    def set_checksum(self, checksum) -> None:
        self.context._set_cell_checksum(self.node_path, self.owner_path, checksum)

    def build(self, input_ref: Any = _UNSET):
        return self.context._build_cell_expression(self.node_path, self.owner_path, input_ref)


class StandaloneCellPins:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}


class WorkflowTransformerPins:
    def __init__(self, context, node_path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_node_path", node_path)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._context._get_transformer_pin_value(self._node_path, (name,))

    def __getitem__(self, key: str):
        return self._context._get_transformer_pin_value(self._node_path, (str(key),))

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self.__setitem__(name, value)

    def __setitem__(self, key: str, value: Any) -> None:
        self._context._set_transformer_pin(self._node_path, (str(key),), value)

    def __delattr__(self, name: str) -> None:
        if name.startswith("_"):
            object.__delattr__(self, name)
            return
        self.__delitem__(name)

    def __delitem__(self, key: str) -> None:
        self._context._delete_transformer_pin(self._node_path, (str(key),))


class WorkflowCelltypes:
    def __init__(self, context, node_path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_node_path", node_path)

    @property
    def _cfg(self):
        return self._context._graph.nodes[self._node_path].transformer_config

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._cfg.celltypes[name]

    def __getitem__(self, key: str):
        return self._cfg.celltypes[str(key)]

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self.__setitem__(name, value)

    def __setitem__(self, key: str, value: Any) -> None:
        self._cfg.celltypes[str(key)] = str(value)
        if key != "result":
            self._cfg.pins.add(str(key))
        self._context._derive_all()


class BoundTransformerBackend:
    def __init__(self, context, node_path: tuple[str, ...]) -> None:
        self.context = context
        self.node_path = node_path

    @property
    def cfg(self):
        return self.context._graph.nodes[self.node_path].transformer_config

    @property
    def language(self):
        return self.cfg.language

    @language.setter
    def language(self, value):
        self.cfg.language = "python" if value is None else value
        self.context._derive_all()

    @property
    def code(self):
        return self.cfg.code

    @code.setter
    def code(self, value):
        self.context._set_transformer_code(self.node_path, value)

    @property
    def pins(self):
        return WorkflowTransformerPins(self.context, self.node_path)

    args = pins

    @property
    def celltypes(self):
        return WorkflowCelltypes(self.context, self.node_path)

    @property
    def optional_pins(self):
        return self.cfg.optional_pins

    @optional_pins.setter
    def optional_pins(self, value):
        self.cfg.optional_pins = set(value or ())
        self.context._derive_all()

    @property
    def modules(self):
        return self.cfg.modules

    @property
    def globals(self):
        return self.cfg.globals

    @property
    def environment(self):
        return self.cfg.environment

    @property
    def meta(self):
        return self.cfg.meta

    @meta.setter
    def meta(self, value):
        self.cfg.meta.update(value)

    @property
    def scratch(self):
        return self.cfg.scratch

    @scratch.setter
    def scratch(self, value):
        self.cfg.scratch = bool(value)

    @property
    def direct_print(self):
        return self.cfg.direct_print

    @direct_print.setter
    def direct_print(self, value):
        self.cfg.direct_print = bool(value)

    @property
    def local(self):
        return self.cfg.local

    @local.setter
    def local(self, value):
        self.cfg.local = value
        self.cfg.meta["local"] = value

    def call(self, *args, **kwargs):
        cfg = self.cfg
        signature = None
        if callable(cfg.callable):
            import inspect

            signature = inspect.signature(cfg.callable)
        if signature is not None:
            bound = signature.bind_partial(*args, **kwargs).arguments
            for name, value in bound.items():
                self.context._set_transformer_pin(self.node_path, (name,), value)
        elif args:
            raise TypeError("No function signature: positional arguments not supported")
        else:
            for name, value in kwargs.items():
                self.context._set_transformer_pin(self.node_path, (name,), value)
        return self.context._compute_node(self.node_path)


__all__ = [
    "BoundCellBackend",
    "BoundTransformerBackend",
    "StandaloneCellPins",
    "WorkflowCelltypes",
    "WorkflowTransformerPins",
]
