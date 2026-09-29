"""Durable graph preparation, entirely before publication or reference release."""
import copy
from seamless import Buffer, Checksum
from .configuration import fingerprint
from .graph import ContextGraph, Node, CellConfig, TransformerConfig, ConstantProducer, Edge
from .errors import PathError, DependencyError


def _parse_path_string(path):
    import ast

    if path == "":
        return ()
    node = ast.parse("x" + path, mode="eval").body
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


def _projected_celltype(celltype, path):
    for component in path:
        if isinstance(component, slice):
            continue
        if celltype == "deepcell":
            celltype = "mixed"
        elif celltype in {"deepfolder", "folder"}:
            celltype = "bytes"
    return celltype


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
    for symbol, anonymous in anonymous_nodes.items():
        if not isinstance(symbol, str) or re.fullmatch(r"[0-9a-f]{5}(?:-[1-9][0-9]*)?", symbol) is None:
            raise PathError(f"Invalid anonymous node symbol: {symbol!r}")
        if not isinstance(anonymous, dict) or set(anonymous) != {"source", "celltype", "path"}:
            raise PathError(f"Invalid anonymous node entry: {symbol!r}")
        source_ref = anonymous["source"]
        if not isinstance(source_ref, dict) or set(source_ref) != {"node"}:
            raise PathError(f"Anonymous node source must name a node: {symbol!r}")
        source_node = tuple(source_ref["node"])
        if source_node not in graph.nodes:
            raise PathError(f"Anonymous node source does not exist: {source_node!r}")
        try:
            path = _parse_path_string(anonymous["path"])
        except (SyntaxError, TypeError, ValueError) as exc:
            raise PathError(f"Invalid anonymous node path: {anonymous['path']!r}") from exc
        celltype = anonymous["celltype"]
        Buffer._map_celltype(celltype)
        graph.anonymous_symbol_by_recipe[(source_node, celltype, path)] = symbol
    for entry in data.get('connections',[]):
        source_ref = entry['source']
        target = tuple(entry['target'])
        source_celltype = None
        source_conversion = False
        source_conversion_before = False
        source_conversion_steps = ()
        source_miswired = False
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
                try:
                    anonymous = anonymous_nodes[symbol]
                except KeyError as exc:
                    raise PathError(f"Unknown anonymous node symbol: {symbol!r}") from exc
                source_node = tuple(anonymous["source"]["node"])
                anonymous_path = _parse_path_string(anonymous["path"])
                source = source_node + anonymous_path + source_path
                root_type = graph.nodes[source_node].cell_config.celltype
                projected_type = _projected_celltype(root_type, anonymous_path)
                anonymous_type = anonymous["celltype"]
                if anonymous_path and anonymous_type != projected_type:
                    # A stored projection cannot change celltype behind the path.
                    source_celltype = anonymous_type
                    source_miswired = True
                elif anonymous_path:
                    source_celltype = anonymous_type
                    converted_type = entry.get("source_conversion_after")
                    if converted_type is not None:
                        source_conversion = True
                        source_conversion_steps = ((len(anonymous_path), converted_type),)
                        source_celltype = converted_type
                else:
                    source_celltype = anonymous_type
                    if anonymous_type != root_type:
                        source_conversion = True
                        source_conversion_before = True
                        source_conversion_steps = ((0, anonymous_type),)
            else:
                raise PathError(f"Invalid connection source reference: {source_ref!r}")
        else:
            source = tuple(source_ref)
        try:
            sn,sl = graph.resolve_existing(source); tn,tl = graph.resolve_existing(target)
        except KeyError as exc:
            raise PathError(f'Connection endpoint does not exist: {exc}') from exc
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
                                source_miswired))
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
