"""Content type selection for shared cell values."""

from __future__ import annotations

import mimetypes
import re


_MIME_TOKEN = r"[A-Za-z0-9!#$%&'*+.^_`|~-]+"
_MIME_VALUE = re.compile(
    rf"^{_MIME_TOKEN}/{_MIME_TOKEN}"
    rf"(?:\s*;\s*{_MIME_TOKEN}\s*=\s*(?:{_MIME_TOKEN}|\"[^\"]*\"))*$"
)


def content_type(celltype, path, explicit=None):
    """Return a MIME type including UTF-8 charset for textual values."""
    if explicit is not None:
        if not isinstance(explicit, str):
            raise ValueError(f"invalid MIME type: {explicit!r}")
        value = explicit.strip()
        if not _MIME_VALUE.fullmatch(value):
            raise ValueError(f"invalid MIME type: {explicit!r}")
    elif celltype == "text":
        value = mimetypes.guess_type(path)[0] or "text/plain"
    elif celltype == "bytes":
        value = mimetypes.guess_type(path)[0] or "application/octet-stream"
    elif celltype in {"str", "plain", "int", "float", "bool", "structured", "json"}:
        value = "application/json"
    elif celltype in {"python", "ipython"}:
        value = "text/x-python"
    elif celltype == "yaml":
        value = "application/yaml"
    else:
        value = "application/octet-stream"
    if value.lower().startswith("text/") and "charset=" not in value.lower():
        value += "; charset=utf-8"
    return value


def is_binary(value):
    """Whether browsers should treat a MIME type as opaque bytes."""
    base = value.split(";", 1)[0].strip().lower()
    if base.startswith("text/"):
        return False
    if base in {
        "application/json",
        "application/javascript",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    }:
        return False
    return not (base.endswith("+json") or base.endswith("+xml"))
