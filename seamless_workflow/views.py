"""Namespace helpers that do not duplicate canonical Cell/Transformer handles."""

from __future__ import annotations


class MissingView:
    def __init__(self, context, path):
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_path", tuple(path))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._context._lookup(self._path + (name,))

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self._context._assign(self._path + (name,), value)

    def __delattr__(self, name):
        self._context._delete(self._path + (name,))

    def __getitem__(self, key):
        return self._context._lookup(self._path + (str(key),))

    def __setitem__(self, key, value):
        self._context._assign(self._path + (str(key),), value)

    def __delitem__(self, key):
        self._context._delete(self._path + (str(key),))

    def __repr__(self):
        return f"<MissingView path={self._path!r}>"


class SubContextView(MissingView):
    @property
    def _prefix(self):
        return self._path


__all__ = ["MissingView", "SubContextView"]
