"""Durable graph preparation, entirely before publication or reference release."""
import copy
from seamless import Buffer, Checksum
from .configuration import fingerprint
from .graph import ContextGraph, Node, CellConfig, TransformerConfig, ConstantProducer, Edge
from .errors import PathError, DependencyError


def prepare_graph(data):
    version = data.get('__seamless_workflow__', '0.2')
    if version not in {'0.2', '0.3', '0.4'}:
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
    for entry in data.get('connections',[]):
        source,target = tuple(entry['source']),tuple(entry['target'])
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
        graph.edges.append(Edge(source,target))
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
