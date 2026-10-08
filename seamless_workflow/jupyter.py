"""Optional Jupyter widget helpers for workflow Context cells."""


def _cell_context(cell):
    backend = getattr(cell, "_workflow_backend", None)
    if backend is None:
        from .errors import NodeError
        from .views import MissingView

        if isinstance(cell, MissingView):
            raise NodeError("Mounts require an existing whole cell node")
        raise AttributeError("Widgets attach to whole Context cell nodes")
    if getattr(backend, "local_path", ()) or getattr(backend, "readonly", False):
        raise AttributeError("Widgets attach to whole Context cell nodes")
    context, path = backend.context, backend.node_path
    context._check_public_caller()
    return context, path


def traitlet(cell):
    """Return the shared traitlets hub attached to a whole Context cell."""
    from .attachments.traitlet import CellTraitlet
    from .attachments.widget import WidgetDriver

    context, path = _cell_context(cell)
    service = context._controller.call("_mount_transport", path, klass=4)
    if service is not None:
        if isinstance(service, WidgetDriver) and isinstance(service.widget, CellTraitlet):
            return service.widget
        raise ValueError("Cell is already mounted; unmount first")

    hub = CellTraitlet(cell, context, path)
    try:
        hub._attach("w")
    except ValueError:
        # Another caller may have attached the hub between lookup and attach.
        service = context._controller.call("_mount_transport", path, klass=4)
        if isinstance(service, WidgetDriver) and isinstance(service.widget, CellTraitlet):
            return service.widget
        raise
    return hub


def output(cell, layout=None, mimetype=None):
    """Return an Output widget observing the cell's shared traitlets hub."""
    from .attachments.output_widget import OutputWidget

    hub = traitlet(cell)
    return OutputWidget(hub, cell.celltype, layout=layout, mimetype=mimetype)
