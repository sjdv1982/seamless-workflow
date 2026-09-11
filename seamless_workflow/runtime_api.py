"""Controller-side read and barrier predicates. Waiting is exclusively caller-side."""
import copy
from concurrent.futures import Future
from seamless import Checksum, Expression
from .sidework import Lease
from .errors import ClosedContextError, StaleWorkflowHandleError, NodeError


class RuntimeAPI:
    def _register_turn_log(self, log):
        self._controller.turn_logs.add(log)

    def _unregister_turn_log(self, log):
        self._controller.turn_logs.discard(log)

    def _read_snapshot(self, path, local=()):
        if path not in self._graph.nodes:
            raise StaleWorkflowHandleError(f'Endpoint {path!r} is stale')
        node = self._graph.nodes[path]
        # A pending node has no published result. Attribute reads never wait
        # for its computation; compute/computation supply that explicit wait.
        return Lease(self._get_checksum(path, ()), self._node_celltype(path))

    def _install_wait(self, path=None, local=(), *, read=False, barrier=False):
        future = Future()
        self._barriers[future] = (path, tuple(local), read, barrier)
        self._check_barriers()
        return future

    def _withdraw_wait(self, future):
        self._barriers.pop(future, None)

    def _check_barriers(self):
        for future, predicate in list(self._barriers.items()):
            if hasattr(predicate, 'check'):
                if future.done():
                    self._barriers.pop(future, None)
                    continue
                ready, result = predicate.check(self)
                if ready:
                    future.set_result(result)
                    self._barriers.pop(future, None)
                continue
            path, local, read, barrier = predicate
            if future.done():
                self._barriers.pop(future, None)
                continue
            if path is not None and path not in self._graph.nodes:
                future.set_exception(StaleWorkflowHandleError(f'Endpoint {path!r} is stale'))
                self._barriers.pop(future, None)
                continue
            paths = self._graph.nodes if path is None else {path} | self._upstream_cone(path)
            if barrier and any(self._graph.nodes[p].state in {'waiting', 'computing'} for p in paths):
                continue
            if read and barrier:
                node = self._graph.nodes[path]
                if node.state in {'unwired','blocked','failed'}:
                    future.set_exception(copy.deepcopy(node.exception) if node.state == 'failed' else NodeError(f'Node is {node.state}: {node.block_reason}'))
                    self._barriers.pop(future, None)
                    continue
            if read:
                node = self._graph.nodes[path]
                checksum = self._get_checksum(path, ())
                if checksum is None:
                    if node.state in {'waiting', 'computing'}:
                        continue
                result = Lease(checksum, self._node_celltype(path))
            else:
                result = None
            future.set_result(result)
            self._barriers.pop(future, None)

    def _snapshot_transformer(self, path, *, concrete_args=None, concrete_code=None):
        from seamless_transformer.builder_snapshot import TransformerBuilderSnapshot
        from .builder_state import _path_string
        import inspect
        node = self._graph.nodes[path]
        cfg = node.transformer_config
        args = {} if concrete_args is None else dict(concrete_args)
        for pin in cfg.pins if concrete_args is None else ():
            edge = self._incoming_edge(path, (pin,))
            if edge is not None:
                args[pin] = self._build_source_expression(edge.source)
            elif pin in node.transformer_pin_producers:
                producer = node.transformer_pin_producers[pin]
                args[pin] = Expression(producer.checksum, celltype=producer.celltype,
                                       target_celltype=cfg.celltypes.get(pin, 'mixed'))
        code = concrete_code if concrete_code is not None else cfg.code_checksum
        code_edge = self._incoming_edge(path, ('code',))
        if concrete_code is None and code_edge is not None:
            state, code = self._source_state(code_edge)
            if state != 'complete':
                raise NodeError('Transformer code is not available')
        claims = [code, *cfg.modules.values()]
        if concrete_args is not None: claims.extend(args.values())
        leases = tuple(Lease(cs) for cs in claims if isinstance(cs, Checksum))
        return TransformerBuilderSnapshot(
            codebuf=code, language=cfg.language, celltypes=copy.deepcopy(cfg.celltypes),
            optional_pins=frozenset(cfg.optional_pins), args=args,
            modules=copy.deepcopy(cfg.modules), globals=copy.deepcopy(cfg.globals),
            meta=copy.deepcopy(cfg.meta), environment=copy.deepcopy(cfg.environment),
            scratch=cfg.scratch, direct_print=cfg.direct_print, local=cfg.local,
            call_mode=cfg.call_mode, callable=cfg.callable,
            schema=cfg.schema, compilation=copy.deepcopy(cfg.compilation), objects=copy.deepcopy(cfg.objects), header=cfg.header,
            signature=inspect.signature(cfg.callable) if callable(cfg.callable) else None,
            leases=leases)
