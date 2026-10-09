"""Public process-wide HTTP/WebSocket share server controls."""

from .attachments.share import server as _server


def configure(host="0.0.0.0", port=5813, **kwargs):
    return _server.configure(host=host, port=port, **kwargs)


def close():
    return _server.close()


def openapi():
    """Return the live process-wide OpenAPI document without a request."""
    from .attachments.share.openapi import build_openapi

    server = _server.get_server_if_started()
    if server is None or server._closed:
        return build_openapi(())
    return server.openapi()


def __getattr__(name):
    if name not in {"host", "port", "url"}:
        raise AttributeError(name)
    server = _server.get_server_if_started()
    if server is not None:
        return getattr(server, name)
    if name == "url":
        return None
    return _server._default_config[name]


def __dir__():
    return sorted(set(globals()) | {"host", "port", "url"})
