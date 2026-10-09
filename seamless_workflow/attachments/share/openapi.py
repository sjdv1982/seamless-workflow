"""OpenAPI snapshots for the live share registry."""

import copy
from urllib.parse import quote


_CELLTYPE_SCHEMAS = {
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "str": {"type": "string"},
    "plain": {},
    "text": {"type": "string"},
    "python": {"type": "string"},
    "ipython": {"type": "string"},
    "yaml": {"type": "string"},
}

_ERROR_SCHEMA = {
    "type": "object",
    "properties": {"error": {"type": "string"}},
    "required": ["error"],
    "additionalProperties": False,
}


def _content(registration, *, nullable=False):
    content_type = registration.content_type.split(";", 1)[0].strip()
    if registration.spec.mimetype is not None:
        schema = {"type": "string"} if not registration.binary else None
    else:
        schema = copy.deepcopy(_CELLTYPE_SCHEMAS.get(registration.celltype))
    if nullable and schema is not None:
        schema = {"anyOf": [schema, {"type": "null"}]}
    media = {} if schema is None else {"schema": schema}
    return {content_type: media}


def _json_error_response(description):
    return {
        "description": description,
        "content": {"application/json": {"schema": copy.deepcopy(_ERROR_SCHEMA)}},
    }


def _get_responses(registration):
    content = _content(registration)
    content.setdefault(
        "text/plain",
        {"schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
    )
    return {
        "200": {
            "description": "Current value bytes; mode=checksum returns checksum text.",
            "headers": _record_headers(),
            "content": content,
        },
        "204": {
            "description": "The shared cell value is null.",
            "headers": _record_headers(),
        },
        "304": {
            "description": "The If-None-Match checksum is current.",
            "headers": _record_headers(),
        },
        "400": _json_error_response("The mode parameter is invalid."),
        "404": _json_error_response("The share does not exist or has no value yet."),
        "503": _json_error_response("The current value bytes cannot be resolved."),
    }


def _head_responses():
    return {
        "200": {
            "description": "The current value is available; no response body is sent.",
            "headers": _record_headers(),
        },
        "204": {
            "description": "The shared cell value is null; no response body is sent.",
            "headers": _record_headers(),
        },
        "304": {
            "description": "The If-None-Match checksum is current; no response body is sent.",
            "headers": _record_headers(),
        },
        "400": {"description": "The mode parameter is invalid; no response body is sent."},
        "404": {"description": "The share does not exist or has no value yet; no response body is sent."},
    }


def _put_responses():
    written = {
        "type": "object",
        "properties": {
            "checksum": {"type": "string"},
            "marker": {"type": "integer", "minimum": 0},
        },
        "required": ["checksum", "marker"],
        "additionalProperties": False,
    }
    conflict = {
        "type": "object",
        "properties": {
            "checksum": {"type": ["string", "null"]},
            "marker": {"type": "integer", "minimum": 0},
        },
        "required": ["checksum", "marker"],
        "additionalProperties": False,
    }
    return {
        "200": {
            "description": "The value was installed; checksum and current marker are returned.",
            "content": {"application/json": {"schema": written}},
        },
        "400": _json_error_response("The mode or marker parameter is invalid."),
        "404": _json_error_response("The share does not exist or its Context is closing."),
        "405": {
            "description": "The share is read-only.",
            "headers": {"Allow": {"schema": {"type": "string"}}},
        },
        "409": {
            "description": "The expected marker is stale; the current record is returned.",
            "content": {"application/json": {"schema": conflict}},
        },
        "413": _json_error_response("The request body exceeds the configured limit."),
        "422": _json_error_response("The body is not a value of the shared cell type."),
        "503": _json_error_response("The Context did not install the observation in time."),
    }


def _record_headers():
    return {
        "ETag": {
            "description": "Quoted checksum of the currently served value.",
            "schema": {"type": "string"},
        },
        "X-Seamless-Marker": {
            "description": "Monotonic version of this share URL.",
            "schema": {"type": "integer", "minimum": 0},
        },
        "Cache-Control": {
            "schema": {"type": "string", "enum": ["no-cache"]},
        },
        "Content-Type": {"schema": {"type": "string"}},
    }


def _parameters(include_marker=False):
    parameters = [
        {
            "name": "mode",
            "in": "query",
            "required": False,
            "description": "Use checksum to return the current checksum as text.",
            "schema": {"type": "string", "enum": ["value", "checksum"]},
        },
        {
            "name": "If-None-Match",
            "in": "header",
            "required": False,
            "schema": {"type": "string"},
        },
    ]
    if include_marker:
        parameters = [
            {
                "name": "mode",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "enum": ["value"]},
            },
            {
                "name": "marker",
                "in": "query",
                "required": False,
                "description": "Accept the write only if this equals the current marker.",
                "schema": {"type": "integer", "minimum": 0},
            },
        ]
    return parameters


def _operation_extensions(registration):
    return {
        "x-seamless-celltype": registration.celltype,
        "x-seamless-node": list(getattr(registration, "node_path", ()) or ()),
    }


def _path_item(registration):
    extensions = _operation_extensions(registration)
    item = {
        "get": {
            **extensions,
            "operationId": f"getShare{registration.session_id}",
            "description": "Read the last complete value served by this cell.",
            "parameters": _parameters(),
            "responses": _get_responses(registration),
        },
        "head": {
            **extensions,
            "operationId": f"headShare{registration.session_id}",
            "description": "Inspect the share without resolving value bytes.",
            "parameters": _parameters(),
            "responses": _head_responses(),
        },
    }
    if not registration.spec.readonly:
        item["put"] = {
            **extensions,
            "operationId": f"putShare{registration.session_id}",
            "description": "Write a value into the shared cell.",
            "parameters": _parameters(include_marker=True),
            "requestBody": {
                "required": False,
                "description": (
                    "Bytes canonicalized as a value of the cell type. An omitted or empty "
                    "body, or the literal null value, writes null."
                ),
                "content": _content(registration, nullable=True),
            },
            "responses": _put_responses(),
        }
    return item


def build_openapi(registrations):
    """Build a document from one immutable snapshot of active registrations."""
    active = tuple(
        registration for registration in registrations
        if registration.active and not registration.closed and not registration.staged
    )
    active = tuple(sorted(active, key=lambda registration: registration.route))
    paths = {registration.route: _path_item(registration) for registration in active}
    namespaces = sorted({registration.namespace for registration in active})
    updates = {
        namespace: "/" + quote(namespace, safe="-._~")
        for namespace in namespaces
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Seamless shared cells",
            "version": "1.0",
            "description": (
                "The namespace update stream is a server-to-client WebSocket at "
                "GET /<namespace>. It sends a handshake, a full share snapshot, "
                "and updates as shared values change."
            ),
        },
        "paths": paths,
        "x-seamless-updates": updates,
    }
