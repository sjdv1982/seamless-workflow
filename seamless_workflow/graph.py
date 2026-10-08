"""Durable graph structures for workflow Context."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from seamless import Checksum

NodeKind = Literal["cell", "transformer"]
NodeState = Literal["miswired", "unwired", "blocked", "waiting", "computing", "complete", "failed"]
BlockReason = Literal["blocked-by-miswiring", "blocked-by-unwired", "blocked-by-error"]
NodePath = tuple[str, ...]
Path = tuple[Any, ...]
ViewPath = tuple[Any, ...]


@dataclass
class ConstantProducer:
    checksum: Checksum
    celltype: str = "mixed"


@dataclass
class CellConfig:
    celltype: str = "mixed"
    validator: Any = None
    validator_language: str | None = None
    scratch: bool = False


@dataclass
class TransformerConfig:
    config_token: str = ""
    schema: str | None = None
    compilation: Any = None
    objects: Any = None
    header: str | None = None
    code: Any = None
    code_checksum: Checksum | None = None
    language: str = "python"
    callable: Any = None
    pins: set[str] = field(default_factory=set)
    celltypes: dict[str, str] = field(default_factory=lambda: {"result": "mixed"})
    optional_pins: set[str] = field(default_factory=set)
    modules: dict[str, Any] = field(default_factory=dict)
    globals: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    environment: Any = None
    scratch: bool = False
    local: bool | None = None
    direct_print: bool = False
    # Operational only: passed on to each fired Transformation, never part of demand.
    streaming: bool = False
    call_mode: Literal["delayed", "direct"] = "delayed"

    def signature_parameters(self) -> set[str] | None:
        """Parameter names the code accepts, or None when unconstrained.

        A transformer whose code is a Python callable has a fixed input signature,
        exactly as a standalone builder does.  Text/bash code has none, so any pin
        name may be declared.
        """

        if self.compilation is not None:
            return set(self.pins)
        if not callable(self.callable):
            return None
        try:
            from seamless_transformer.optional_pins import pin_signature
            return set(pin_signature(inspect.signature(self.callable)).parameters)
        except (TypeError, ValueError):
            return None

    def check_pin_name(self, name: str, *, allow_result: bool = False) -> None:
        """Reject a pin name the transformer signature cannot accept.

        Mirrors the standalone `ArgsWrapper`/`CelltypesWrapper` rule: an already
        declared name is always assignable, an unknown name is only declarable when
        the code has no fixed signature.  Without this, `.pins` silently declares a
        typo as a new pin and every later run fails with an unexpected-keyword
        `TypeError`.
        """

        if name == "result" and not allow_result:
            raise AttributeError("'result' is the transformer result, not an input pin")
        if name in self.celltypes or name in self.pins:
            return
        parameters = self.signature_parameters()
        if parameters is not None and name not in parameters:
            raise AttributeError(
                f"Unknown transformer pin '{name}': the transformer signature has no "
                f"such parameter (declared pins: {', '.join(sorted(self.pins)) or 'none'})"
            )


@dataclass
class Node:
    kind: NodeKind
    state: NodeState = "unwired"
    block_reason: BlockReason | None = None
    block_pins: list[str] = field(default_factory=list)
    pin_block_reasons: dict[str, str] = field(default_factory=dict)
    cell_config: CellConfig | None = None
    transformer_config: TransformerConfig | None = None
    cell_root_producer: ConstantProducer | None = None
    cell_root_expression: object = None
    transformer_pin_producers: dict[str, ConstantProducer] = field(default_factory=dict)
    pin_states: dict[str, tuple] = field(default_factory=dict)
    pin_read_errors: dict[str, tuple] = field(default_factory=dict)
    current_checksum: Checksum | None = None
    exception: BaseException | None = None
    mount: object = None
    mount_inactive: bool = False


@dataclass(frozen=True)
class Edge:
    source: ViewPath
    target: ViewPath
    source_celltype: str | None = None
    source_conversion: bool = False
    source_conversion_before: bool = False
    source_conversion_steps: tuple[tuple[int, str], ...] = ()
    # Miswiring is never stored on an edge: it is worked out from the graph's
    # content wherever the graph is derived (``ContextGraph.edge_miswiring``).
    # The anonymous links of the source recipe, innermost first, as the
    # ``(source, celltype, path)`` recipes whose symbols were assigned when the
    # edge was added (``ContextGraph.register_edge_symbols``).  ``source`` is
    # the source node path for the first link and the previous link's symbol
    # after that.  Empty for a source without a path or a conversion.  Derived
    # bookkeeping, so it takes no part in equality.
    source_chain: tuple[tuple[Any, str, Path], ...] = field(default=(), compare=False)
    deep_member: bool = False


def projected_celltype(celltype: str, path: Path) -> str:
    """The celltype after projecting ``path``: only a step below a deep celltype changes it."""
    for component in path:
        if isinstance(component, slice):
            continue
        if celltype == "deepcell":
            celltype = "mixed"
        elif celltype in {"deepfolder", "folder"}:
            celltype = "bytes"
    return celltype


def link_miswired(source_celltype: str, celltype: str, path: Path) -> bool:
    """Whether one recorded anonymous link is statically ill-formed.

    cells.md, *The handle and the node*: an anonymous cell's ``celltype`` is
    fixed when it is created, and retyping its source never retypes it.  A
    link that carries a path, read over a source whose celltype no longer
    projects to the recorded one, both projects and converts (*Connecting*).
    Over a deep source, the link may take exactly one string-item step
    (deep-celltypes.md, *Paths*); the rest is a separate link.  A pathless
    link is a conversion, which a retype never makes ill-formed here.
    """
    path = tuple(path)
    if not path:
        return False
    if source_celltype in DEEP_CELLTYPES and (len(path) != 1 or not isinstance(path[0], str)):
        return True
    return celltype != projected_celltype(source_celltype, path)


DEEP_CELLTYPES = frozenset({"deepcell", "deepfolder", "folder"})


def anonymous_links(
    source_celltype: str,
    local_path: Path,
    conversion_steps: tuple[tuple[int, str], ...] = (),
) -> list[tuple[str, Path]]:
    """Split a source recipe into its anonymous links, innermost first.

    The recipe is a node of celltype ``source_celltype``, read at
    ``local_path`` with ``conversion_steps`` applied at their path positions.
    Each link is ``(celltype, path)`` and carries either a path or a
    conversion, never both (cells.md, *Connecting*).  A step below a deep
    celltype is a link of its own: it is a fusion barrier, so the path that
    follows it is a separate link over the child (expressions.md, *Fusion*).
    """
    links = []
    current = source_celltype
    position = 0

    def path_links(segment):
        nonlocal current
        if current in DEEP_CELLTYPES and len(segment) > 1:
            current = projected_celltype(current, segment[:1])
            links.append((current, segment[:1]))
            segment = segment[1:]
        current = projected_celltype(current, segment)
        links.append((current, segment))

    for step_position, converted in sorted(conversion_steps, key=lambda step: step[0]):
        step_position = min(max(step_position, position), len(local_path))
        segment = tuple(local_path[position:step_position])
        if segment:
            path_links(segment)
        links.append((converted, ()))
        current = converted
        position = step_position
    segment = tuple(local_path[position:])
    if segment:
        path_links(segment)
    return links


def fusible_runs(
    source_celltype: str,
    links: list[tuple[str, Path]],
) -> list[tuple[str, Path, str, int]]:
    """Group anonymous links into maximal fusible runs (expressions.md, *Fusion*).

    ``links`` is ``anonymous_links``' answer for a recipe read at
    ``source_celltype``.  Returns ``(input_celltype, path, celltype, last)``
    per run, innermost first: the run is one Expression over the previous
    run's result (the recipe's root for the first), and ``last`` is the index
    of its last link.  The anonymous cells of the links before ``last`` are
    inside the run: elided, never built (cells.md, *Anonymous cells, symbols
    and elision*).
    """
    from seamless.checksum.conversion import conversion_reinterpret, conversion_trivial

    preserving = conversion_trivial | conversion_reinterpret
    runs = []
    # The open run: [input, path, celltype, last, ends_in_conversion, absorbed]
    open_run = None

    def close():
        nonlocal open_run
        if open_run is not None:
            runs.append(tuple(open_run[:4]))
            open_run = None

    current = source_celltype
    for index, (celltype, path) in enumerate(links):
        path = tuple(path)
        if current in DEEP_CELLTYPES or celltype in DEEP_CELLTYPES:
            close()
            runs.append((current, path, celltype, index))
        elif path:
            if open_run is None:
                open_run = [current, path, celltype, index, False, False]
            elif not open_run[4]:
                open_run[1] = open_run[1] + path
                open_run[2] = celltype
                open_run[3] = index
            else:
                run_input, run_path, run_type = open_run[:3]
                if (not run_path and not open_run[5]
                        and (run_input == run_type
                             or (run_input, run_type) in preserving)):
                    open_run = [run_type, path, celltype, index, False, True]
                else:
                    close()
                    open_run = [current, path, celltype, index, False, False]
        else:
            if open_run is None:
                open_run = [current, (), celltype, index, True, False]
            elif celltype == current:
                open_run[3] = index
            elif open_run[4]:
                close()
                open_run = [current, (), celltype, index, True, False]
            else:
                open_run[2] = celltype
                open_run[3] = index
                open_run[4] = True
        current = celltype
    close()
    return runs


def split_deep_step(
    celltype: str,
    local_path: Path,
    conversion_steps: tuple[tuple[int, str], ...] = (),
):
    """Split a source recipe at its deep step (deep-celltypes.md, *Paths*).

    Nothing converts into a deep celltype, so a recipe takes at most one step
    below a deep celltype, and it is the first path component, read after the
    conversions at position 0.  Returns ``None`` when the recipe takes no such
    step.  Otherwise returns ``(prefix, deep, member, rest_path, rest_steps)``:
    ``prefix`` are the celltypes of the conversions applied before the step,
    ``deep`` is the celltype the step reads, ``member`` the celltype it yields,
    and ``rest_path`` / ``rest_steps`` are the recipe that follows the step.
    The step is a fusion barrier: the rest is a separate recipe over the child
    checksum, at ``member`` (expressions.md, *Fusion*).
    """
    local_path = tuple(local_path)
    if not local_path:
        return None
    # Stable by position: conversions at one position apply in the order written.
    steps = sorted(conversion_steps, key=lambda step: step[0])
    prefix = tuple(converted for position, converted in steps if position <= 0)
    current = prefix[-1] if prefix else celltype
    if current not in DEEP_CELLTYPES:
        return None
    member = projected_celltype(current, local_path[:1])
    rest_steps = tuple((position - 1, converted) for position, converted in steps if position > 0)
    return prefix, current, member, local_path[1:], rest_steps


def deep_barrier(celltype: str, path: Path, target_type: str):
    """The member celltype when ``path`` over ``celltype`` must stop at a deep step.

    A one-step path below a deep celltype is one Expression when its target is
    the member celltype or ``checksum`` (deep-celltypes.md, *What the one step
    yields*).  Anything after the step -- a further path, or the child's own
    conversion -- is a separate Expression over the child checksum
    (expressions.md, *Fusion*).  Returns ``None`` when ``path`` stays one
    Expression.
    """
    path = tuple(path)
    if celltype not in DEEP_CELLTYPES or not path:
        return None
    member = projected_celltype(celltype, path[:1])
    if member in DEEP_CELLTYPES:
        # Not a string item: the step itself is refused at construction.
        return None
    if len(path) == 1 and target_type in {member, "checksum"}:
        return None
    return member


def deep_recipe_error(
    root_type: str,
    local_path: Path,
    conversion_steps: tuple[tuple[int, str], ...] = (),
):
    """The error of a deep link outside the deep tables, or ``None``.

    cells.md, *Connecting*: where a link's source is deep, what is legal is the
    table of deep-celltypes.md -- the zero-path deep conversions, and exactly
    one string-item step yielding the member celltype -- and a link outside it
    is statically ill-formed.  The ordinary wiring rule governs everything
    after the step.  The error is the Expression constructor's ``ValueError``.
    """
    from seamless.checksum.expression import validate_expression_shape
    from .builder_state import _path_string

    local_path = tuple(local_path)
    deep = split_deep_step(root_type, local_path, conversion_steps)
    if deep is not None:
        _prefix, deep_type, member, _rest, _rest_steps = deep
        try:
            validate_expression_shape(_path_string(local_path[:1]), deep_type, member)
        except (TypeError, ValueError) as exc:
            return exc
    current, position = root_type, 0
    for step_position, converted in sorted(conversion_steps, key=lambda step: step[0]):
        step_position = min(max(step_position, position), len(local_path))
        current = projected_celltype(current, local_path[position:step_position])
        position = step_position
        if converted != current and (current in DEEP_CELLTYPES or converted in DEEP_CELLTYPES):
            try:
                validate_expression_shape("", current, converted)
            except (TypeError, ValueError) as exc:
                return exc
        current = converted
    return None


def _symbol_base(recipe) -> str:
    """The five hex characters of an anonymous node's symbol (cells.md, *Symbols*).

    Module-level so that tests can force two recipes to collide.
    """
    return hashlib.sha1(repr(recipe).encode("utf-8")).hexdigest()[:5]


class ContextGraph:
    def __init__(self) -> None:
        self.nodes: dict[NodePath, Node] = {}
        self.edges: list[Edge] = []
        self.incoming: dict[NodePath, set[int]] = {}
        self.outgoing: dict[NodePath, set[int]] = {}
        self.namespaces: set[NodePath] = set()
        self.anonymous_symbol_by_recipe: dict[tuple, str] = {}

    def rebuild_indexes(self) -> None:
        self.incoming = {path: set() for path in self.nodes}
        self.outgoing = {path: set() for path in self.nodes}
        for index, edge in enumerate(self.edges):
            source_node, _ = self.resolve_existing(edge.source)
            target_node, _ = self.resolve_existing(edge.target)
            self.outgoing.setdefault(source_node, set()).add(index)
            self.incoming.setdefault(target_node, set()).add(index)

    def resolve_existing(self, view_path: ViewPath) -> tuple[NodePath, Path]:
        for size in range(len(view_path), 0, -1):
            node_path = tuple(view_path[:size])
            if node_path in self.nodes:
                return node_path, tuple(view_path[size:])
        raise KeyError(view_path)

    def has_prefix(self, prefix: NodePath) -> bool:
        return any(path[: len(prefix)] == prefix for path in self.nodes) or prefix in self.namespaces

    def descendants(self, prefix: NodePath) -> list[NodePath]:
        return sorted(path for path in self.nodes if path[: len(prefix)] == prefix)

    def first_free(self, stem: str) -> NodePath:
        index = 1
        while (f"{stem}{index}",) in self.nodes or (f"{stem}{index}",) in self.namespaces:
            index += 1
        return (f"{stem}{index}",)

    def node_celltype(self, path: NodePath) -> str:
        node = self.nodes[path]
        if node.kind == "cell":
            return node.cell_config.celltype
        return node.transformer_config.celltypes.get("result", "mixed")

    def assign_symbol(self, recipe) -> str:
        """Return the symbol of ``recipe``, assigning one if it has none.

        cells.md, *Symbols*: an existing symbol never changes; a new recipe
        whose five hex characters are taken by a different recipe is suffixed
        ``-1``, ``-2``, ... in arrival order.
        """
        symbols = self.anonymous_symbol_by_recipe
        symbol = symbols.get(recipe)
        if symbol is not None:
            return symbol
        used = set(symbols.values())
        base = _symbol_base(recipe)
        symbol = base
        suffix = 1
        while symbol in used:
            symbol = f"{base}-{suffix}"
            suffix += 1
        symbols[recipe] = symbol
        return symbol

    def assign_chain(self, source_node: NodePath, links) -> tuple:
        """Assign symbols to the links of a chain from ``source_node``, innermost first.

        Returns the links' recipes, ``(source, celltype, path)``, where
        ``source`` is ``source_node`` for the first link and the previous
        link's symbol after that.
        """
        recipes = []
        source_key = tuple(source_node)
        for celltype, path in links:
            recipe = (source_key, celltype, tuple(path))
            source_key = self.assign_symbol(recipe)
            recipes.append(recipe)
        return tuple(recipes)

    def register_edge_symbols(self, edge: Edge) -> Edge:
        """Assign the symbols of every anonymous link of ``edge``'s source, in order.

        cells.md, *Symbols*: a symbol is assigned when its anonymous cell is
        created, which is when the first edge referring to its recipe is
        added.  Every place that adds an edge calls this.  Returns the edge
        carrying its chain of recipes (``Edge.source_chain``).
        """
        if edge.source_chain:
            for recipe in edge.source_chain:
                self.assign_symbol(recipe)
            return edge
        source_node, local = self.resolve_existing(edge.source)
        steps = ()
        if edge.source_conversion:
            steps = edge.source_conversion_steps
            if not steps:
                position = 0 if edge.source_conversion_before else len(local)
                steps = ((position, edge.source_celltype or self.node_celltype(source_node)),)
        links = anonymous_links(self.node_celltype(source_node), local, steps)
        if not links:
            return edge
        return replace(edge, source_chain=self.assign_chain(source_node, links))

    def symbol_of(self, recipe) -> str:
        """Look up the symbol of a registered recipe.  A miss is an internal error."""
        symbol = self.anonymous_symbol_by_recipe.get(recipe)
        assert symbol is not None, (
            f"anonymous recipe {recipe!r} has no symbol: an edge was added "
            "without register_edge_symbols()"
        )
        return symbol

    def chain_miswired(self, chain) -> bool:
        """Whether any recorded link of an anonymous chain is ill-formed.

        ``chain`` is an edge's ``source_chain``: ``(source, celltype, path)``
        recipes, innermost first.  The first link reads a named node at that
        node's *current* celltype; every later link reads the previous link's
        *recorded* celltype, since an anonymous cell's celltype is fixed when
        it is created (cells.md, *The handle and the node*).  Used through
        ``edge_miswiring``.
        """
        source_celltype = None
        for source_key, celltype, path in chain:
            if isinstance(source_key, tuple):
                if source_key not in self.nodes:
                    return False
                source_celltype = self.node_celltype(source_key)
            if link_miswired(source_celltype, celltype, path):
                return True
            source_celltype = celltype
        return False

    def edge_target_celltype(self, edge: Edge) -> str | None:
        """The celltype at an edge's target: a cell's (or a join slot's), or a pin's."""
        target_node, target_local = self.resolve_existing(edge.target)
        node = self.nodes[target_node]
        if node.kind == "cell":
            return projected_celltype(node.cell_config.celltype, target_local)
        if len(target_local) == 1 and target_local != ("code",):
            return node.transformer_config.celltypes.get(target_local[0], "mixed")
        return None

    def looks_through(self, edge: Edge) -> bool:
        """Whether ``edge`` is a dummy edge from a single link, looked through.

        cells.md, *The input*: "A dummy edge from a single link is looked
        through"; *Assigning an anonymous handle*: after a reload "`a` holds
        the link itself".  The edge comes from an anonymous cell whose link
        reads directly from a named node, and it is an identity.  A path link
        counts in any case, since the wiring rule makes it an identity
        wherever it can be written (and ``get_graph()`` has always saved it as
        the target's own link).  A chain is never looked through.
        """
        chain = edge.source_chain
        if len(chain) != 1:
            return False
        source_key, celltype, path = chain[0]
        if not isinstance(source_key, tuple) or source_key not in self.nodes:
            return False
        target_node, target_local = self.resolve_existing(edge.target)
        target = self.nodes[target_node]
        if (edge.source_conversion and target_local and target.kind == "cell"
                and target.cell_config.celltype in DEEP_CELLTYPES):
            # A deep slot takes a member checksum, not an implicit root
            # conversion. Preserve the conversion that produces that member.
            return False
        if path and not edge.source_conversion:
            return True
        return not path and celltype == self.edge_target_celltype(edge)

    def reads_as_one_link(self, edge: Edge) -> bool:
        """Whether a looked-through link, as its target's own edge, is still that one link.

        Saved as the target's own edge, a path is re-read over the source's
        *current* celltype, and over a deep source a path of more than one
        step splits into the deep step and a further link (deep-celltypes.md,
        *Paths*).  Such a link -- a retype made its source deep, or a graph
        entry was written that way -- is ill-formed as one link, and
        collapsing it on save would silently make it well-formed.
        """
        source_key, _celltype, path = edge.source_chain[0]
        return not path or len(anonymous_links(self.node_celltype(source_key), path)) == 1

    def edge_miswiring(self, edge: Edge):
        """How ``edge``'s anonymous links leave its target: ``None``, or a state.

        This is the one place miswiring of anonymous links is worked out.  It
        is never stored: a running Context and one rebuilt by ``set_graph``
        derive it from the same recorded entries (``Edge.source_chain``), so
        they agree (node-state-lifecycle.md, *`miswired` is a static defect*).

        - A looked-through single link is the target's own incoming link, so
          its defect is the target's own: ``"miswired"``.  It is judged as an
          own edge is, against the celltype at the target -- a cell's, a join
          slot's or a pin's -- and the source's current celltype; and it is
          ill-formed when it no longer reads as one link.
        - Otherwise an ill-formed anonymous cell anywhere in the chain is
          ``miswired`` itself, and the target it feeds is blocked:
          ``"blocked-by-miswiring"`` (cells.md, *The handle and the node*).
        """
        if self.looks_through(edge):
            if not self.reads_as_one_link(edge):
                return "miswired"
            source_key, _celltype, path = edge.source_chain[0]
            target_celltype = self.edge_target_celltype(edge)
            if (path and target_celltype is not None and target_celltype not in DEEP_CELLTYPES
                    and link_miswired(self.node_celltype(source_key), target_celltype, path)):
                return "miswired"
            return None
        if self.chain_miswired(edge.source_chain):
            return "blocked-by-miswiring"
        return None

    def collapses_on_save(self, edge: Edge) -> bool:
        """Whether ``get_graph()`` saves ``edge`` as its target's own incoming link.

        cells.md, *Which entries are serialized*: "A dummy edge from a single
        link is saved as its target's own incoming link".  A link that would
        not read back as that one link is saved with its entry instead, as a
        chain's dummy edge is, so that the reload looks through the very same
        link.
        """
        return self.looks_through(edge) and self.reads_as_one_link(edge)

    def own_link(self, edge: Edge) -> Edge:
        """``edge`` as its target derives and reports it.

        A looked-through dummy edge is the link itself as the target's own
        incoming edge -- the edge ``get_graph()`` saves and ``set_graph()``
        loads back: no recorded celltype and no conversion, so the target
        reads the link's source and converts into its own celltype.  The
        recorded link stays attached (``source_chain``) for
        ``edge_miswiring`` and for its symbol.  Every other edge is returned
        as is.
        """
        if not self.looks_through(edge):
            return edge
        return Edge(edge.source, edge.target, source_chain=edge.source_chain,
                    deep_member=edge.deep_member)


__all__ = [
    "BlockReason",
    "CellConfig",
    "ConstantProducer",
    "ContextGraph",
    "Edge",
    "Node",
    "NodeKind",
    "NodePath",
    "NodeState",
    "Path",
    "TransformerConfig",
    "ViewPath",
]
