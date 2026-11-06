"""Runtime configuration for a shared Context cell."""

from dataclasses import asdict, dataclass
import re

from .mime import content_type


_SAFE_SEGMENT = re.compile(r"^[^/\\?#\x00-\x1f\x7f]+$")
RESERVED_TOPLEVEL_KEYS = frozenset({"openapi.json", "seamless-client.js"})
STATE_GRAPH_KEY = "state-graph"
RESERVED_NAMESPACE_KEYS = frozenset({STATE_GRAPH_KEY})
SHAREABLE_CELLTYPES = frozenset({
    "text", "python", "ipython", "yaml", "plain", "str", "int", "float",
    "bool", "bytes", "binary", "mixed",
})


def _key_parts(key, what):
    """Split a `/`-separated URL key or path string, refusing unsafe segments."""
    if not isinstance(key, str) or not key:
        raise ValueError(f"{what} must be a nonempty URL key")
    parts = key.split("/")
    if any(not _SAFE_SEGMENT.fullmatch(part) or part in {".", ".."} for part in parts):
        raise ValueError(f"invalid {what}: {key!r}")
    return parts


def validate_celltype(celltype):
    if celltype not in SHAREABLE_CELLTYPES:
        raise TypeError(f"Celltype {celltype!r} is not shareable")


@dataclass(frozen=True)
class ShareSpec:
    path: str
    readonly: bool = True
    mimetype: str | None = None
    toplevel: bool = False

    driver = "share"
    authority = "cell"
    persistent = True

    def __post_init__(self):
        parts = _key_parts(self.path, "share path")
        if type(self.readonly) is not bool:
            raise TypeError("readonly must be bool")
        if type(self.toplevel) is not bool:
            raise TypeError("toplevel must be bool")
        if self.toplevel and len(parts) != 1:
            raise ValueError("a top-level share path must be one URL segment")
        if self.toplevel and self.path in RESERVED_TOPLEVEL_KEYS:
            raise ValueError("top-level route is reserved")
        if not self.toplevel and len(parts) == 1 and self.path in RESERVED_NAMESPACE_KEYS:
            raise ValueError("namespace route is reserved")
        if self.mimetype is not None:
            # Validate the explicit value before a namespace or listener exists.
            content_type("bytes", self.path, self.mimetype)

    @property
    def mode(self):
        return "w" if self.readonly else "rw"

    def to_graph(self):
        return asdict(self)


DOWNLOAD_FORMATS = {
    # format: (content type, file name extension)
    "tar": ("application/x-tar", ".tar"),
    "tar.gz": ("application/gzip", ".tar.gz"),
    "tar.xz": ("application/x-xz", ".tar.xz"),
    "zip": ("application/zip", ".zip"),
}


def _normalize_download_cells(cells):
    """Return sorted ``(member, cell path)`` pairs, checking syntax only."""
    if isinstance(cells, dict):
        pairs = list(cells.items())
    elif isinstance(cells, (list, tuple)):
        pairs = [(cell, cell) for cell in cells]
    else:
        raise TypeError("download cells must be a list of cell paths or a {member: cell path} dict")
    if not all(isinstance(member, str) and isinstance(cell, str) for member, cell in pairs):
        raise TypeError("download member names and cell paths must be strings")
    members = set()
    for member, cell in pairs:
        _key_parts(member, "download member name")
        if _key_parts(cell, "download cell path")[0] == "shares":
            raise ValueError(f"invalid download cell path: {cell!r}")
        if member in members:
            raise ValueError(f"download member {member!r} is named more than once")
        members.add(member)
    for member in members:
        prefix = member + "/"
        if any(other.startswith(prefix) for other in members):
            raise ValueError(f"download member {member!r} is a directory of another member")
    return tuple(sorted(pairs))


@dataclass(frozen=True)
class DownloadSpec:
    """A namespace URL that serves an archive of cell buffers, built per GET."""

    path: str
    cells: tuple
    format: str = "tar.gz"

    toplevel = False
    readonly = True

    def __post_init__(self):
        parts = _key_parts(self.path, "download path")
        if len(parts) == 1 and self.path in RESERVED_NAMESPACE_KEYS:
            raise ValueError("namespace route is reserved")
        if self.format not in DOWNLOAD_FORMATS:
            raise ValueError(
                f"download format must be one of {sorted(DOWNLOAD_FORMATS)}, not {self.format!r}"
            )
        object.__setattr__(self, "cells", _normalize_download_cells(self.cells))

    @property
    def mimetype(self):
        return DOWNLOAD_FORMATS[self.format][0]

    @property
    def filename(self):
        name = self.path.rsplit("/", 1)[-1]
        extension = DOWNLOAD_FORMATS[self.format][1]
        return name if name.endswith(extension) else name + extension

    def to_graph(self):
        if all(member == cell for member, cell in self.cells):
            cells = [cell for _, cell in self.cells]
        else:
            cells = dict(self.cells)
        return {"path": self.path, "cells": cells, "format": self.format}
