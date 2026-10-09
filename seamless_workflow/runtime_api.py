"""Controller-side read and barrier predicates. Waiting is exclusively caller-side."""
import hashlib
import copy
import json
import time
from concurrent.futures import Future
from seamless import Checksum, Expression
from .builder_state import (
    _cell_block_reason,
    _cell_exception,
    _transformer_block_reason,
    _transformer_exception,
)
from .sidework import Lease
from .errors import ClosedContextError, StaleWorkflowHandleError, NodeError


class RuntimeAPI:
    def _state_graph_refresh(self):
        """Controller operation whose turn publishes a state graph snapshot."""

    def _state_graph(self):
        nodes = []
        for path, node in sorted(self._graph.nodes.items()):
            entry = {"type": node.kind, "path": list(path)}
            if node.kind == "cell":
                entry["celltype"] = node.cell_config.celltype
                entry["block_reason"] = _cell_block_reason(node)
                entry["exception"] = _cell_exception(node)
            else:
                entry["language"] = node.transformer_config.language
                entry["block_reason"] = _transformer_block_reason(node)
                entry["exception"] = _transformer_exception(node)
            entry["state"] = node.state
            entry["checksum"] = (
                node.current_checksum.hex()
                if node.current_checksum is not None else None
            )
            nodes.append(entry)
        connections, anonymous_nodes = self._graph_connections()
        return {
            "nodes": nodes,
            "anonymous_nodes": anonymous_nodes,
            "connections": connections,
        }

    def _publish_state_graph(self):
        payload = self._state_graph()
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        previous = getattr(self, "_state_graph_published", None)
        marker = 1 if previous is None else previous[0]
        if previous is None or previous[1] != digest:
            marker += 1 if previous is not None else 0
        publication = (marker, digest, body)
        self._state_graph_published = publication
        self._state_graph_published_at = time.monotonic()
        self._controller.state_graph_dirty = False
        return publication

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

    def _install_wait(self, path=None, local=(), *, read=False, barrier=False,
                      return_none_on_incomplete=False):
        future = Future()
        self._barriers[future] = (
            path, tuple(local), read, barrier, return_none_on_incomplete
        )
        self._check_barriers()
        return future

    def _withdraw_wait(self, future):
        self._barriers.pop(future, None)

    def _node_error_detail(self, path, node):
        if node.kind != "transformer":
            return node.block_reason
        reasons = node.pin_block_reasons
        if node.state == "unwired":
            pin = next((pin for pin, reason in sorted(reasons.items()) if reason == "unwired"), None)
            return f"missing input pin {pin!r}" if pin is not None else node.block_reason
        if node.state == "miswired":
            pin = next((pin for pin, reason in sorted(reasons.items()) if reason == "miswired"), None)
            if pin is not None:
                edge = self._incoming_for(path).get((pin,))
                if edge is not None:
                    source_path, source_local = self._graph.resolve_existing(edge.source)
                    source_type = self._celltype_for_path(source_path, source_local)
                    target_type = node.transformer_config.celltypes.get(pin, "mixed")
                    return f"pin {pin!r} expects celltype {target_type!r}, got {source_type!r}"
                return f"pin {pin!r} has a miswired source"
        return node.block_reason

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
            path, local, read, barrier, return_none_on_incomplete = predicate
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
                if node.state in {'miswired','unwired','blocked','failed'}:
                    if return_none_on_incomplete:
                        future.set_result(None)
                    else:
                        detail = self._node_error_detail(path, node)
                        future.set_exception(copy.deepcopy(node.exception) if node.state == 'failed' else NodeError(f'Node is {node.state}: {detail}'))
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

    def _freeze_transformer(self, path, *, concrete_args=None, concrete_code=None,
                            dispatch_scratch=None):
        from seamless_transformer.frozen_transformer import FrozenTransformer
        from .builder_state import _path_string
        import inspect
        from seamless_transformer.optional_pins import pin_signature
        node = self._graph.nodes[path]
        cfg = node.transformer_config
        signature = pin_signature(inspect.signature(cfg.callable)) if callable(cfg.callable) else None
        if cfg.compilation is not None:
            from seamless_transformer.compiled_validation import validate_stage1
            sig = validate_stage1(cfg.schema, cfg.celltypes, cfg.optional_pins, cfg.meta.get('metavars', {}))
            signature = inspect.Signature([inspect.Parameter(p.name, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                                           for p in sig.inputs])
        args = {} if concrete_args is None else dict(concrete_args)
        input_celltypes = {}
        for pin in cfg.pins if concrete_args is None else ():
            edge = self._incoming_edge(path, (pin,))
            pin_state = node.pin_states.get(pin, ('unwired', None, None))[0]
            if edge is not None:
                if pin_state == 'failed':
                    _, checksum = self._source_state(edge)
                    args[pin] = checksum
                    input_celltypes[pin] = self._edge_input_celltype(edge)
                    continue
                if pin in cfg.optional_pins:
                    from seamless.checksum.null import canonicalize_checksum, is_null
                    state, checksum = self._source_state(edge)
                    if state == 'complete' and checksum is not None:
                        input_type = self._edge_input_celltype(edge)
                        if is_null(canonicalize_checksum(checksum, input_type)):
                            continue
                source = self._build_source_expression(edge.source)
                args[pin] = Expression(source, celltype=cfg.celltypes.get(pin, 'mixed'))
            elif pin in node.transformer_pin_producers:
                producer = node.transformer_pin_producers[pin]
                if pin_state == 'failed':
                    args[pin] = producer.checksum
                    input_celltypes[pin] = producer.celltype
                    continue
                if pin in cfg.optional_pins and producer.checksum is not None:
                    from seamless.checksum.null import canonicalize_checksum, is_null
                    if is_null(canonicalize_checksum(producer.checksum, producer.celltype)):
                        continue
                args[pin] = Expression(producer.checksum, input_celltype=producer.celltype,
                                       celltype=cfg.celltypes.get(pin, 'mixed'))
        code = concrete_code if concrete_code is not None else cfg.code_checksum
        code_edge = self._incoming_edge(path, ('code',))
        if concrete_code is None and code_edge is not None:
            state, code = self._source_state(code_edge)
            if state != 'complete':
                raise NodeError('Transformer code is not available')
        claims = [code, *cfg.modules.values()]
        if concrete_args is not None:
            claims.extend(args.values())
        else:
            claims.extend(
                value for value in args.values() if isinstance(value, Checksum)
            )
        leases = tuple(Lease(cs) for cs in claims if isinstance(cs, Checksum))
        literal_pins = frozenset(
            set(node.transformer_pin_producers) | set(cfg.modules) |
            ({'code'} if code_edge is None else set())
        )
        return FrozenTransformer(
            codebuf=code, language=cfg.language, celltypes=copy.deepcopy(cfg.celltypes),
            optional_pins=frozenset(cfg.optional_pins), args=args,
            modules=copy.deepcopy(cfg.modules), globals=copy.deepcopy(cfg.globals),
            meta=copy.deepcopy(cfg.meta), environment=copy.deepcopy(cfg.environment),
            scratch=(cfg.scratch if dispatch_scratch is None else dispatch_scratch),
            direct_print=cfg.direct_print, local=cfg.local, streaming=cfg.streaming,
            call_mode=cfg.call_mode, callable=cfg.callable,
            schema=cfg.schema, compilation=copy.deepcopy(cfg.compilation), objects=copy.deepcopy(cfg.objects), header=cfg.header,
            signature=signature, input_celltypes=input_celltypes,
            literal_pins=literal_pins,
            leases=leases)
