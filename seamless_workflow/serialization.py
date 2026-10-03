"""Durable graph preparation, entirely before publication or reference release."""
import copy
from seamless import Buffer, Checksum, Expression
from .configuration import fingerprint
from .graph import (
    ContextGraph, Node, CellConfig, TransformerConfig, ConstantProducer, Edge, projected_celltype,
)
from .errors import PathError, DependencyError


def _parse_path_string(path):
    import ast

    if path == "":
        return ()
    relative_path = path if path.startswith((".", "[")) else "." + path
    node = ast.parse("x" + relative_path, mode="eval").body
    components = []
    while not isinstance(node, ast.Name):
        if isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Slice):
                def part(value):
                    return None if value is None else ast.literal_eval(value)
                component = slice(part(key.lower), part(key.upper), part(key.step))
            else:
                component = ast.literal_eval(key)
            components.append(component)
            node = node.value
        elif isinstance(node, ast.Attribute):
            components.append(node.attr)
            node = node.value
        else:
            raise ValueError(f"Invalid path: {path!r}")
    if node.id != "x":
        raise ValueError(f"Invalid path: {path!r}")
    return tuple(reversed(components))


_EXPRESSION_RECIPE_KEYS = frozenset({"input", "path", "input_celltype", "celltype"})
_EXPRESSION_VALIDATOR_KEYS = frozenset({"validator", "validator_language"})


def expression_to_graph(expression):
    # workflow-context.md, *Graph serialization*: a cell's retained root
    # Expression is saved as its definition, nested through ``"input"``.  A
    # chain whose innermost input is a live object (a Transformation) has no
    # durable form: None.
    recipes = []
    ref = expression
    while isinstance(ref, Expression):
        recipes.append(ref)
        ref = ref._input_ref
    if ref is None:
        input_entry = None
    elif isinstance(ref, Checksum):
        input_entry = {"checksum": ref.hex()}
    else:
        return None
    for link in reversed(recipes):
        recipe = {
            "input": input_entry,
            "path": link.path,
            "input_celltype": link.input_celltype,
            "celltype": link.celltype,
        }
        if link.validator is not None or link.validator_language is not None:
            recipe["validator"] = None if link.validator is None else link.validator.hex()
            recipe["validator_language"] = link.validator_language
        input_entry = {"expression": recipe}
    return input_entry["expression"]


def expression_from_graph(recipe):
    import re

    # Walk the nesting iteratively (a hostile graph may nest deeply), checking
    # every level, then build innermost first.
    levels = []
    current = recipe
    while True:
        if not isinstance(current, dict):
            raise PathError(f"Cell expression recipe must be a mapping, not {type(current).__name__}")
        keys = set(current)
        missing = _EXPRESSION_RECIPE_KEYS - keys
        if missing:
            raise PathError(f"Cell expression recipe is missing {sorted(missing)!r}")
        unknown = keys - _EXPRESSION_RECIPE_KEYS - _EXPRESSION_VALIDATOR_KEYS
        if unknown:
            raise PathError(f"Cell expression recipe has unknown keys {sorted(unknown)!r}")
        if ("validator" in keys) != ("validator_language" in keys):
            raise PathError("Cell expression recipe must give validator and validator_language together")
        if not isinstance(current["path"], str):
            raise PathError(f"Cell expression path must be a string: {current['path']!r}")
        for field in ("input_celltype", "celltype"):
            value = current[field]
            if not isinstance(value, str):
                raise PathError(f"Cell expression {field} must be a string: {value!r}")
            try:
                Buffer._map_celltype(value)
            except TypeError as exc:
                raise PathError(f"Cell expression {field} is not a celltype: {value!r}") from exc
        if "validator" in keys:
            validator = current["validator"]
            language = current["validator_language"]
            if validator is not None and (
                not isinstance(validator, str) or re.fullmatch(r"[0-9a-f]{64}", validator) is None
            ):
                raise PathError(f"Cell expression validator is not a checksum: {validator!r}")
            if language is not None and not isinstance(language, str):
                raise PathError(f"Cell expression validator_language must be a string: {language!r}")
        levels.append(current)
        input_entry = current["input"]
        if input_entry is None:
            ref = None
            break
        if not isinstance(input_entry, dict) or len(input_entry) != 1 or not (
            set(input_entry) <= {"checksum", "expression"}
        ):
            raise PathError(
                "Cell expression input must be null, {'checksum': ...} or {'expression': ...}"
            )
        if "checksum" in input_entry:
            text = input_entry["checksum"]
            if not isinstance(text, str) or re.fullmatch(r"[0-9a-f]{64}", text) is None:
                raise PathError(f"Cell expression input is not a checksum: {text!r}")
            ref = Checksum(text)
            break
        current = input_entry["expression"]
    # The stored fields are post-fusion, so rebuilding does not change them.
    for level in reversed(levels):
        kwargs = {}
        if "validator" in level:
            kwargs["validator"] = (
                None if level["validator"] is None else Checksum(level["validator"])
            )
            kwargs["validator_language"] = level["validator_language"]
        try:
            ref = Expression(
                ref, path=level["path"], input_celltype=level["input_celltype"],
                celltype=level["celltype"], **kwargs,
            )
        except (TypeError, ValueError) as exc:
            raise PathError(f"Invalid cell expression recipe: {exc}") from exc
    return ref


def prepare_graph(data):
    version = data.get('__seamless_workflow__', '0.2')
    if version not in {'0.2', '0.3', '0.4', '0.5'}:
        raise PathError(f'Unsupported workflow graph version: {version!r}')
    graph = ContextGraph()
    for entry in data.get('nodes', []):
        path = tuple(entry['path'])
        if not path or path in graph.nodes:
            raise PathError(f'Duplicate or empty node path: {path!r}')
        if 'overlay' in entry or 'overlays' in entry:
            raise PathError('Alpha overlay graph data is not supported')
        if entry['type'] == 'cell':
            ct = entry.get('celltype', 'mixed')
            Buffer._map_celltype(ct)
            legacy_target = entry.get('target_celltype', ct)
            if legacy_target != ct or (version == '0.4' and 'target_celltype' in entry):
                raise PathError('Legacy target_celltype differs from celltype; convert the graph explicitly')
            cfg = CellConfig(ct, entry.get('validator'), entry.get('validator_language'),
                             bool(entry.get('scratch', False)))
            value = entry.get('value')
            producer = None if value is None else ConstantProducer(Checksum(value['checksum']),value.get('celltype',ct))
            node = Node('cell', cell_config=cfg, cell_root_producer=producer)
            recipe = entry.get('expression')
            if recipe is not None:
                # workflow-context.md, *Graph serialization*: a cell holds a
                # literal or a root Expression recipe, not both.
                if version != '0.5':
                    raise PathError('Cell expression recipes require workflow graph version 0.5')
                if value is not None:
                    raise PathError('A cell entry cannot have both a value and an expression')
                node.cell_root_expression = expression_from_graph(recipe)
        elif entry['type'] == 'transformer':
            if 'call_mode' not in entry:
                raise PathError('Transformer graph entry is missing required call_mode')
            code = entry.get('code')
            buf = Buffer(code, 'python' if entry.get('language','python') == 'python' else 'text') if code is not None else None
            code_cs = entry.get('checksum',{}).get('code')
            cfg = TransformerConfig(
                code=buf if buf is not None else (Checksum(code_cs) if code_cs else None),
                code_checksum=Checksum(code_cs) if code_cs else (buf.get_checksum() if buf is not None else None),
                language=entry.get('language','python'), pins=set(entry.get('pins',{})),
                celltypes={**{p:v.get('celltype','mixed') for p,v in entry.get('pins',{}).items()},
                           'result':entry.get('result_celltype','mixed')},
                optional_pins=set(entry.get('optional_pins',())),
                modules=copy.deepcopy(entry.get('modules',{})), globals=copy.deepcopy(entry.get('globals',{})),
                meta=copy.deepcopy(entry.get('meta',{})), environment=copy.deepcopy(entry.get('environment')),
                scratch=entry.get('scratch',False), local=entry.get('local'), direct_print=entry.get('direct_print',False),
                call_mode=entry['call_mode'], schema=entry.get('schema'),
                compilation=copy.deepcopy(entry.get('compilation')), objects=copy.deepcopy(entry.get('objects')), header=entry.get('header'))
            if cfg.compilation is not None:
                if cfg.optional_pins:
                    raise TypeError('compiled inputs cannot be optional')
                import yaml
                from seamless_signature import Signature, generate_header
                from seamless_transformer.compiled_validation import validate_declarations
                try:
                    sig = Signature.from_dict(yaml.safe_load(cfg.schema))
                    cfg.header = generate_header(sig)
                except Exception:
                    cfg.header = None
                else:
                    cfg.pins = {p.name for p in sig.inputs}
                    cfg.celltypes = {**{p: cfg.celltypes.get(p, 'mixed') for p in cfg.pins},
                                     'result': cfg.celltypes.get('result', 'mixed')}
                    validate_declarations(sig, cfg.celltypes, warn=True)
            fingerprint(cfg)
            producers = {p:ConstantProducer(Checksum(q['checksum']),q.get('celltype',cfg.celltypes.get(p,'mixed')))
                         for p,q in entry.get('producers',{}).items()}
            node = Node('transformer', transformer_config=cfg, transformer_pin_producers=producers)
        else:
            raise PathError(f'Unknown node type: {entry["type"]!r}')
        if path == ('mounts',): raise PathError('mounts is a reserved Context API name')
        if 'mount' in entry:
            if node.kind != 'cell': raise PathError('Only cells may have mount specs')
            try:
                from .attachments.spec import AttachmentSpec, validate_celltype
                if set(entry['mount']) - {'path', 'mode', 'authority', 'persistent'}:
                    raise ValueError('Unknown mount spec fields')
                node.mount = AttachmentSpec(**entry['mount'])
                validate_celltype(cfg.celltype, node.mount.mode)
            except (TypeError, ValueError) as exc: raise PathError(f'Invalid mount spec: {exc}') from exc
        graph.nodes[path] = node
    anonymous_nodes = data.get("anonymous_nodes", {})
    if not isinstance(anonymous_nodes, dict):
        raise PathError("anonymous_nodes must be a mapping")
    if version != "0.5" and anonymous_nodes:
        raise PathError("anonymous_nodes require workflow graph version 0.5")
    import re
    parsed_anonymous_nodes = {}
    for symbol, anonymous in anonymous_nodes.items():
        if not isinstance(symbol, str) or re.fullmatch(r"[0-9a-f]{5}(?:-[1-9][0-9]*)?", symbol) is None:
            raise PathError(f"Invalid anonymous node symbol: {symbol!r}")
        if not isinstance(anonymous, dict) or set(anonymous) != {"source", "celltype", "path"}:
            raise PathError(f"Invalid anonymous node entry: {symbol!r}")
        source_ref = anonymous["source"]
        if not isinstance(source_ref, dict) or set(source_ref) not in ({"node"}, {"symbol"}):
            raise PathError(f"Invalid anonymous node source: {symbol!r}")
        if set(source_ref) == {"node"}:
            source_key = tuple(source_ref["node"])
            if source_key not in graph.nodes:
                raise PathError(f"Anonymous node source does not exist: {source_key!r}")
        else:
            source_key = source_ref["symbol"]
            if not isinstance(source_key, str):
                raise PathError(f"Invalid anonymous node source symbol: {source_key!r}")
        try:
            path = _parse_path_string(anonymous["path"])
        except (SyntaxError, TypeError, ValueError) as exc:
            raise PathError(f"Invalid anonymous node path: {anonymous['path']!r}") from exc
        celltype = anonymous["celltype"]
        Buffer._map_celltype(celltype)
        graph.anonymous_symbol_by_recipe[(source_key, celltype, path)] = symbol
        parsed_anonymous_nodes[symbol] = (source_ref, celltype, path)

    resolved_anonymous_nodes = {}
    resolving_anonymous_nodes = set()

    def resolve_anonymous_node(symbol):
        if symbol in resolved_anonymous_nodes:
            return resolved_anonymous_nodes[symbol]
        if symbol in resolving_anonymous_nodes:
            raise DependencyError(f"Anonymous node cycle at {symbol!r}")
        try:
            source_ref, celltype, path = parsed_anonymous_nodes[symbol]
        except KeyError as exc:
            raise PathError(f"Unknown anonymous node symbol: {symbol!r}") from exc
        resolving_anonymous_nodes.add(symbol)
        if set(source_ref) == {"node"}:
            source_node = tuple(source_ref["node"])
            source_path = ()
            source_celltype = graph.node_celltype(source_node)
            conversion_steps = ()
            chain = ()
            source_key = source_node
        else:
            (source_node, source_path, source_celltype,
             conversion_steps, chain) = resolve_anonymous_node(source_ref["symbol"])
            source_key = source_ref["symbol"]
        # The entry's recorded celltype is kept in the edge's chain.  Whether
        # the link is ill-formed is not decided here: it is derived from the
        # chain, by the same function a running Context uses
        # (``ContextGraph.edge_miswiring``; node-state-lifecycle.md,
        # *`miswired` is a static defect*: set_graph derives the state).
        chain = chain + ((source_key, celltype, path),)
        source_path = source_path + path
        if not path and celltype != source_celltype:
            conversion_steps = conversion_steps + ((len(source_path), celltype),)
        resolved = (
            source_node,
            source_path,
            celltype,
            conversion_steps,
            chain,
        )
        resolving_anonymous_nodes.remove(symbol)
        resolved_anonymous_nodes[symbol] = resolved
        return resolved

    for symbol in parsed_anonymous_nodes:
        resolve_anonymous_node(symbol)
    for entry in data.get('connections',[]):
        source_ref = entry['source']
        target = tuple(entry['target'])
        source_celltype = None
        source_conversion = False
        source_conversion_before = False
        source_conversion_steps = ()
        source_chain = ()
        if isinstance(source_ref, dict):
            source_path = tuple(entry.get("source_path", ()))
            if set(source_ref) == {"node"}:
                source = tuple(source_ref["node"]) + source_path
                steps = entry.get("source_conversion_steps")
                if steps:
                    source_conversion_steps = tuple(
                        (int(position), celltype) for position, celltype in steps
                    )
                    source_conversion = True
                    source_conversion_before = source_conversion_steps[0][0] == 0
                    source_celltype = source_conversion_steps[-1][1]
            elif set(source_ref) == {"symbol"}:
                symbol = source_ref["symbol"]
                (source_node, anonymous_path, anonymous_type,
                 source_conversion_steps, source_chain) = resolve_anonymous_node(symbol)
                source = source_node + anonymous_path + source_path
                source_celltype = projected_celltype(anonymous_type, source_path)
                if source_path:
                    source_chain = source_chain + ((symbol, source_celltype, source_path),)
                if source_conversion_steps:
                    source_conversion = True
                    source_conversion_before = source_conversion_steps[0][0] == 0
            else:
                raise PathError(f"Invalid connection source reference: {source_ref!r}")
        else:
            source = tuple(source_ref)
        try:
            sn,sl = graph.resolve_existing(source); tn,tl = graph.resolve_existing(target)
        except KeyError as exc:
            raise PathError(f'Connection endpoint does not exist: {exc}') from exc
        if graph.nodes[tn].kind == 'cell' and graph.nodes[tn].cell_root_expression is not None:
            raise PathError(f'Connection targets a cell with a root expression: {target!r}')
        if graph.nodes[tn].mount and 'r' in graph.nodes[tn].mount.mode:
            raise PathError('Sensing mounts cannot have incoming connections')
        if graph.nodes[tn].kind == 'cell':
            if len(tl)>1 or any(isinstance(p,slice) for p in tl):
                raise PathError('Cell graph targets are limited to root or one point component')
        elif len(tl)!=1:
            raise PathError('Transformer graph targets must address one whole pin')
        if any(edge.target == target for edge in graph.edges):
            raise DependencyError(f'Multiple producers for {target!r}')
        graph.edges.append(Edge(source, target, source_celltype, source_conversion,
                                source_conversion_before, source_conversion_steps,
                                source_chain=source_chain))
    # cells.md, *Symbols*: entries loaded from 0.5 keep their stored symbols;
    # the links of every other edge (0.4 graphs, and single links saved as the
    # target's own incoming edge) get theirs now, in file order.
    graph.edges = [graph.register_edge_symbols(edge) for edge in graph.edges]
    visiting,done=set(),set()
    def visit(path):
        if path in visiting: raise DependencyError('Dependency cycle')
        if path in done: return
        visiting.add(path)
        for edge in graph.edges:
            source,_=graph.resolve_existing(edge.source);target,_=graph.resolve_existing(edge.target)
            if target==path: visit(source)
        visiting.remove(path);done.add(path)
    for path in graph.nodes: visit(path)
    return graph
