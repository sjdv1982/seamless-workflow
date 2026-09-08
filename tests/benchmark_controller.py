"""Controller round-trip probes required by design section 12.1.

Run from this directory with: conda run -n seamless1 python benchmark_controller.py
"""
from time import perf_counter
from seamless_workflow import Context


def measure(label, count, operation):
    start = perf_counter()
    operation()
    elapsed = perf_counter() - start
    print(f'{label}: {count} operations, {elapsed:.3f} s, {elapsed/count*1000:.3f} ms/op', flush=True)


def main():
    with Context() as ctx:
        measure('individual graph construction', 1000,
                lambda: [setattr(ctx, f'a{i}', i) for i in range(1000)])
        graph = ctx.get_graph()
        with Context() as clone:
            measure('bulk set_graph (1000 nodes)', 1, lambda: clone.set_graph(graph))
        handle = ctx.a0
        measure('value reads', 1000, lambda: [handle.value for _ in range(1000)])
        measure('state reads', 1000, lambda: [handle.state for _ in range(1000)])
    with Context() as ctx:
        def interactive():
            for i in range(100):
                ctx.a = i
                assert ctx.a.value == i
        measure('interactive write/read pairs', 100, interactive)


if __name__ == '__main__':
    main()
