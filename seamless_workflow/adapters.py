"""Narrow substrate adapters used by the workflow layer."""

from __future__ import annotations

from typing import Any

from seamless import Buffer, Checksum


def checksum_for_value(
    value: Any, celltype: str = "mixed", *, checksum_is_value: bool = False
) -> Checksum:
    """Serialize a Python value and return its checksum.

    An explicit Checksum is already serialized state, and is returned as it is.
    The exception is a written value (``checksum_is_value``) of celltype
    ``checksum``: there a Checksum is the value itself.
    """

    # Ordinary 64-character strings are never reinterpreted as checksums.
    if isinstance(value, Checksum) and not (checksum_is_value and celltype == "checksum"):
        return value
    buffer = Buffer(value, celltype)
    checksum = buffer.get_checksum()
    buffer.tempref()
    return checksum


def buffer_for_checksum(checksum: Checksum | str | bytes | None):
    if checksum is None:
        return None
    return Checksum(checksum).resolve()


def value_for_checksum(checksum: Checksum | str | bytes | None, celltype: str = "mixed"):
    if checksum is None:
        return None
    return Checksum(checksum).resolve(celltype)


def normalize_checksum(checksum: Checksum | str | bytes) -> Checksum:
    return Checksum(checksum)


__all__ = [
    "checksum_for_value",
    "buffer_for_checksum",
    "value_for_checksum",
    "normalize_checksum",
]
