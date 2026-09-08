"""Durable graph preparation, entirely before publication or reference release."""
import copy
from seamless import Buffer, Checksum
from .configuration import fingerprint
from .graph import ContextGraph, Node, CellConfig, TransformerConfig, ConstantProducer, Edge
from .errors import PathError, DependencyError


def prepare_graph(data):
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
            cfg = CellConfig(ct, entry.get('target_celltype',ct), entry.get('validator'), entry.get('validator_language'))
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
            fingerprint(cfg)
            producers = {p:ConstantProducer(Checksum(q['checksum']),q.get('celltype',cfg.celltypes.get(p,'mixed')))
                         for p,q in entry.get('producers',{}).items()}
            node = Node('transformer', transformer_config=cfg, transformer_pin_producers=producers)
        else:
            raise PathError(f'Unknown node type: {entry["type"]!r}')
        graph.nodes[path] = node
    for entry in data.get('connections',[]):
        source,target = tuple(entry['source']),tuple(entry['target'])
        try:
            sn,sl = graph.resolve_existing(source); tn,tl = graph.resolve_existing(target)
        except KeyError as exc:
            raise PathError(f'Connection endpoint does not exist: {exc}') from exc
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
