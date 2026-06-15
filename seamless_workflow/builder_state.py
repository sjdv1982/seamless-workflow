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


__all__ = ["BoundCellBackend", "StandaloneCellPins"]
