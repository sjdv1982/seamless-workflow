"""Content type selection for shared cell values."""

from __future__ import annotations

import mimetypes
import re


TEXT_TYPES = set("""
    application/json
    application/xml
    application/xml-dtd
    application/yaml
    application/x-yaml
    application/toml
    application/javascript
    application/ecmascript
    application/x-javascript
    application/x-www-form-urlencoded
    application/graphql
    application/sql
    application/sparql-query
    application/trig
    application/n-triples
    application/n-quads
    application/sdp
    application/x-ndjson
    application/x-sh
    application/x-shellscript
    application/x-httpd-php
    application/x-perl
    application/x-tex
    application/x-latex
    application/vnd.apple.mpegurl
    application/x-mpegurl

    chemical/x-pdb
    chemical/x-cif
    chemical/x-mmcif
    chemical/x-cml
    chemical/x-mdl-molfile
    chemical/x-mdl-sdfile
    chemical/x-mdl-rxnfile
    chemical/x-mol2
    chemical/x-xyz
    chemical/x-daylight-smiles
    chemical/x-jcamp-dx
    chemical/x-gaussian-input
    chemical/x-gaussian-cube
    chemical/x-ncbi-asn1-ascii

    model/obj
    model/mtl
    model/vrml
    model/iges
    model/step

    image/x-xbitmap
    image/x-xpixmap
    audio/mpegurl
    audio/x-mpegurl
    audio/x-scpls
""".split())

BINARY_TYPES = set("""
    chemical/x-cdx
    chemical/x-ncbi-asn1-binary
    model/gltf-binary
    model/3mf
    model/step+zip
    model/step-xml+zip
    text/1d-interleaved-parityfec
    text/rtp-enc-aescm128
    text/red
    text/rtx
    application/pdf
    application/zip
    application/gzip
    application/wasm
    application/cbor
    application/x-protobuf
    application/vnd.google.protobuf
""".split())

MIXED_TYPES = set("""
    text/rtf
    application/rtf
    application/postscript
    application/octet-stream
    model/stl
    image/x-portable-bitmap
    image/x-portable-graymap
    image/x-portable-pixmap
    image/x-portable-anymap
""".split())

TEXT_SUFFIXES = (
    "+json", "+xml", "+yaml", "+csv",
)

BINARY_SUFFIXES = (
    "+cbor", "+cbor-seq", "+der", "+wbxml",
    "+zip", "+gzip", "+zstd", "+fastinfoset",
    "+cose", "+cwt", "+uper",
)


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


def classify_mime(content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()

    if "/" not in mime:
        return "unknown"

    # Exact exceptions take precedence.
    if mime in MIXED_TYPES:
        return "unknown"
    if mime in BINARY_TYPES:
        return "binary"
    if mime in TEXT_TYPES:
        return "text"

    # Structured syntax suffixes work across top-level types.
    if mime.endswith(BINARY_SUFFIXES):
        return "binary"
    if mime.endswith(TEXT_SUFFIXES):
        return "text"

    # General MIME families.
    if mime.startswith("text/"):
        return "text"

    if mime.startswith(("image/", "audio/", "video/", "font/")):
        return "binary"

    # chemical/, model/, application/, message/, multipart/, etc.
    return "unknown"


def is_text_mime(content_type: str) -> bool:
    return classify_mime(content_type) == "text"


def is_binary(value):
    """Whether browsers should treat a MIME type as opaque bytes.

    Unknown and ambiguous formats remain binary to avoid lossy text decoding.
    """
    return classify_mime(value) != "text"
