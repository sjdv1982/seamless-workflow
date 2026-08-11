"""Normalized identities exchanged by Context wiring and bound builders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


EndpointKind = Literal[
    "cell-result",
    "transformer-result",
    "cell-subvalue",
    "transformer-input",
    "transformer-code",
]


@dataclass(frozen=True)
class BoundEndpoint:
    top_id: str
    node_path: tuple[str, ...]
    endpoint_kind: EndpointKind
    local_path: tuple[Any, ...] = ()
    can_source: bool = True
    can_target: bool = False
    can_set: bool = False


__all__ = ["BoundEndpoint", "EndpointKind"]
