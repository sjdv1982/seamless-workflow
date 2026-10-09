"""Runtime configuration for a shared Context cell."""

from dataclasses import asdict, dataclass
import re

from .mime import content_type


_SAFE_SEGMENT = re.compile(r"^[^/\\?#\x00-\x1f\x7f]+$")
RESERVED_TOPLEVEL_KEYS = frozenset({"openapi.json", "seamless-client.js"})
SHAREABLE_CELLTYPES = frozenset({
    "text", "python", "ipython", "yaml", "plain", "str", "int", "float",
    "bool", "bytes", "binary", "mixed",
})


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
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("share path must be a nonempty URL key")
        parts = self.path.split("/")
        if any(not _SAFE_SEGMENT.fullmatch(part) or part in {".", ".."} for part in parts):
            raise ValueError(f"invalid share path: {self.path!r}")
        if type(self.readonly) is not bool:
            raise TypeError("readonly must be bool")
        if type(self.toplevel) is not bool:
            raise TypeError("toplevel must be bool")
        if self.toplevel and len(parts) != 1:
            raise ValueError("a top-level share path must be one URL segment")
        if self.toplevel and self.path in RESERVED_TOPLEVEL_KEYS:
            raise ValueError("top-level route is reserved")
        if self.mimetype is not None:
            # Validate the explicit value before a namespace or listener exists.
            content_type("bytes", self.path, self.mimetype)

    @property
    def mode(self):
        return "w" if self.readonly else "rw"

    def to_graph(self):
        return asdict(self)
