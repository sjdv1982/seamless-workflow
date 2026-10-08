"""ipywidgets Output adapter for a workflow cell's traitlets hub."""

import json


_MIMETYPES = {"text/plain", "text/html", "application/json", "image/png"}


class OutputWidget:
    def __init__(self, hub, celltype, *, layout=None, mimetype=None):
        if mimetype is not None and mimetype not in _MIMETYPES:
            raise ValueError(f"Unsupported mimetype: {mimetype}")
        try:
            from ipywidgets import Output
            from IPython.display import Code, HTML, Image, JSON, Pretty
        except ImportError as exc:
            raise ImportError("Output widgets require ipywidgets and IPython") from exc

        self.output_instance = Output() if layout is None else Output(layout=layout)
        self._celltype = celltype
        self._mimetype = mimetype
        self._display_types = (Code, HTML, Image, JSON, Pretty)
        self._hub = hub
        self._handler = self._render
        hub.observe(self._handler, names="value")

    def _display_value(self, value):
        Code, HTML, Image, JSON, Pretty = self._display_types
        if self._mimetype == "text/plain":
            return Pretty(str(value))
        if self._mimetype == "text/html":
            return HTML(str(value))
        if self._mimetype == "application/json":
            return JSON(value)
        if self._mimetype == "image/png":
            return Image(value, format="png")
        if self._celltype in {"text", "str", "int", "float", "bool", "yaml"}:
            return Pretty(str(value))
        if self._celltype == "plain":
            return Pretty(json.dumps(value, sort_keys=True, indent=2))
        if self._celltype in {"python", "ipython"}:
            return Code(str(value), language="python")
        return Pretty(repr(value))

    def _render(self, change):
        value = change["new"]
        if value is None:
            return
        self.output_instance.outputs = ()
        self.output_instance.append_display_data(self._display_value(value))

    def __getattr__(self, name):
        if name.startswith("_repr_") or name.startswith("_ipython_"):
            return getattr(self.output_instance, name)
        raise AttributeError(name)

    def _ipython_display_(self):
        from IPython.display import display
        return display(self.output_instance)

    def _repr_mimebundle_(self, include=None, exclude=None):
        return self.output_instance._repr_mimebundle_(include=include, exclude=exclude)
