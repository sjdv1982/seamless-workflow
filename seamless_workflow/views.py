"""Ephemeral public Context views."""

from __future__ import annotations

from typing import Any, Iterator


class MissingView:
    def __init__(self, context, path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._context._view(self._path + (name,))

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._context._assign(self._path + (name,), value)

    def __delattr__(self, name: str) -> None:
        self._context._delete(self._path + (name,))

    def __getitem__(self, key: str):
        return self._context._view(self._path + (str(key),))

    def __setitem__(self, key: str, value: Any) -> None:
        self._context._assign(self._path + (str(key),), value)

    def __delitem__(self, key: str) -> None:
        self._context._delete(self._path + (str(key),))

    def __repr__(self) -> str:
        return f"<MissingView path={self._path!r}>"


class SubContextView(MissingView):
    @property
    def _prefix(self):
        return self._path


class CellView:
    def __init__(self, context, node_path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_node_path", node_path)

    @property
    def pins(self):
        return CellPinsView(self._context, self._node_path)

    @property
    def checksum(self):
        return self._context._get_checksum(self._node_path, ())

    @property
    def buffer(self):
        return self._context._get_buffer(self._node_path, ())

    @property
    def value(self):
        return self._context._get_value(self._node_path, ())

    @property
    def celltype(self):
        return self._context._graph.nodes[self._node_path].cell_config.celltype

    @celltype.setter
    def celltype(self, value):
        self._context._set_cell_config(self._node_path, celltype=value)

    @property
    def target_celltype(self):
        return self._context._graph.nodes[self._node_path].cell_config.target_celltype

    @target_celltype.setter
    def target_celltype(self, value):
        self._context._set_cell_config(self._node_path, target_celltype=value)

    def set(self, value):
        self._context._set_cell_value(self._node_path, (), value)

    def set_checksum(self, checksum):
        self._context._set_cell_checksum(self._node_path, (), checksum)

    def compute(self):
        return self._context._compute_node(self._node_path)

    run = compute

    def clear_exception(self):
        return self._context._clear_exception(self._node_path)

    def prune(self):
        return self._context.prune(self._node_path)

    def __getitem__(self, key: str):
        return CellPinView(self._context, self._node_path, (str(key),))

    def __setitem__(self, key: str, value):
        if value is None:
            self._context._delete_cell_path(self._node_path, (str(key),))
        else:
            self._context._set_cell_value(self._node_path, (str(key),), value)

    def __delitem__(self, key: str) -> None:
        self._context._delete_cell_path(self._node_path, (str(key),))

    def __repr__(self) -> str:
        return f"<CellView path={self._node_path!r}>"


class CellPinsView:
    def __init__(self, context, node_path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_node_path", node_path)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return CellPinView(self._context, self._node_path, (name,))

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self.__setitem__(name, value)

    def __delattr__(self, name: str) -> None:
        self.__delitem__(name)

    def __getitem__(self, key: str):
        return CellPinView(self._context, self._node_path, (str(key),))

    def __setitem__(self, key: str, value):
        path = (str(key),)
        if value is None:
            self._context._delete_cell_path(self._node_path, path)
        elif self._context._is_bound_source(value):
            self._context._add_edge(self._context._source_path(value), self._node_path + path)
        else:
            self._context._set_cell_value(self._node_path, path, value)

    def __delitem__(self, key: str) -> None:
        self._context._delete_cell_path(self._node_path, (str(key),))

    def update(self, mapping) -> None:
        constants = {}
        for key, value in mapping.items():
            path = (str(key),)
            if self._context._is_bound_source(value):
                self._context._add_edge(self._context._source_path(value), self._node_path + path)
            else:
                constants[path] = value
        if constants:
            self._context._set_cell_values(self._node_path, constants)


class CellPinView:
    def __init__(self, context, node_path: tuple[str, ...], path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_node_path", node_path)
        object.__setattr__(self, "_path", path)

    @property
    def checksum(self):
        return self._context._get_checksum(self._node_path, self._path)

    @property
    def value(self):
        return self._context._get_value(self._node_path, self._path)

    def set(self, value):
        if self._context._is_bound_source(value):
            self._context._add_edge(self._context._source_path(value), self._node_path + self._path)
        else:
            self._context._set_cell_value(self._node_path, self._path, value)

    def set_checksum(self, checksum):
        self._context._set_cell_checksum(self._node_path, self._path, checksum)

    def __getattr__(self, name: str):
        raise AttributeError(
            "Cell subcell paths are limited to one level; use cell.value for resolved data"
        )

    def __repr__(self) -> str:
        return f"<CellPinView node={self._node_path!r} path={self._path!r}>"


class TransformerView:
    def __init__(self, context, node_path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_node_path", node_path)

    @property
    def result(self):
        return TransformerResultView(self._context, self._node_path)

    @property
    def code(self):
        return TransformerPinView(self._context, self._node_path, ("code",))

    @code.setter
    def code(self, value):
        self._context._set_transformer_code(self._node_path, value)

    @property
    def inp(self):
        return TransformerInputView(self._context, self._node_path)

    @property
    def pins(self):
        return TransformerPinsView(self._context, self._node_path)

    @property
    def scratch(self):
        return self._context._graph.nodes[self._node_path].transformer_config.scratch

    @scratch.setter
    def scratch(self, value):
        self._context._graph.nodes[self._node_path].transformer_config.scratch = bool(value)

    def __getitem__(self, key: str):
        return TransformerPinView(self._context, self._node_path, (str(key),))

    def __setitem__(self, key: str, value):
        path = (str(key),)
        if self._context._is_bound_source(value):
            self._context._add_edge(self._context._source_path(value), self._node_path + path)
        else:
            self._context._set_transformer_pin(self._node_path, path, value)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        cfg = self._context._graph.nodes[self._node_path].transformer_config
        if name in cfg.pins and not hasattr(type(self), name):
            return TransformerPinView(self._context, self._node_path, (name,))
        raise AttributeError(name)

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if name == "scratch":
            self._context._graph.nodes[self._node_path].transformer_config.scratch = bool(value)
            return
        cfg = self._context._graph.nodes[self._node_path].transformer_config
        if name in cfg.pins and not hasattr(type(self), name):
            self._context._set_transformer_pin(self._node_path, (name,), value)
            return
        raise AttributeError(name)

    def compute(self):
        return self._context._compute_node(self._node_path)

    run = compute

    def clear_exception(self):
        return self._context._clear_exception(self._node_path)


class TransformerResultView(CellView):
    @property
    def checksum(self):
        return self._context._get_checksum(self._node_path, ())

    @property
    def value(self):
        node = self._context._graph.nodes[self._node_path]
        celltype = node.transformer_config.celltypes.get("result", "mixed")
        return self._context._get_value(self._node_path, (), celltype=celltype)

    def set(self, value):
        raise AttributeError("Transformer result is produced by the transformer")


class TransformerInputView:
    def __init__(self, context, node_path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_node_path", node_path)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return TransformerPinView(self._context, self._node_path, (name,))

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        path = (name,)
        if self._context._is_bound_source(value):
            self._context._add_edge(self._context._source_path(value), self._node_path + path)
        else:
            self._context._set_transformer_pin(self._node_path, path, value)


class TransformerPinsView(TransformerInputView):
    def __getitem__(self, key: str):
        return TransformerPinView(self._context, self._node_path, (str(key),))

    def __setitem__(self, key: str, value):
        path = (str(key),)
        if self._context._is_bound_source(value):
            self._context._add_edge(self._context._source_path(value), self._node_path + path)
        else:
            self._context._set_transformer_pin(self._node_path, path, value)


class TransformerPinView:
    def __init__(self, context, node_path: tuple[str, ...], path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_node_path", node_path)
        object.__setattr__(self, "_path", path)

    @property
    def checksum(self):
        return self._context._get_transformer_pin_checksum(self._node_path, self._path)

    @property
    def value(self):
        return self._context._get_transformer_pin_value(self._node_path, self._path)

    def set(self, value):
        if self._context._is_bound_source(value):
            self._context._add_edge(self._context._source_path(value), self._node_path + self._path)
        else:
            self._context._set_transformer_pin(self._node_path, self._path, value)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return TransformerPinView(self._context, self._node_path, self._path + (name,))

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        path = self._path + (name,)
        if self._context._is_bound_source(value):
            self._context._add_edge(self._context._source_path(value), self._node_path + path)
        else:
            self._context._set_transformer_pin(self._node_path, path, value)


__all__ = [
    "CellPinsView",
    "CellPinView",
    "CellView",
    "MissingView",
    "SubContextView",
    "TransformerInputView",
    "TransformerPinsView",
    "TransformerPinView",
    "TransformerResultView",
    "TransformerView",
]
