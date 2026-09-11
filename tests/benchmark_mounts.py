"""Local mount throughput/latency probe; no timing-based correctness assertions."""
import json
import statistics
import tempfile
import time
from pathlib import Path
from seamless import Cell
from seamless_workflow import Context
from seamless_workflow.attachments.fs.service import get_service


def benchmark(count=100, rounds=5):
    with tempfile.TemporaryDirectory() as tmp, Context() as ctx:
        paths=[]
        started=time.perf_counter()
        for n in range(count):
            path=Path(tmp)/str(n);path.write_text('initial')
            ctx[str(n)]=Cell(celltype='text')
            ctx[str(n)].mount(path,mode='r')
            paths.append(path)
        attach=time.perf_counter()-started
        latencies=[]
        for round_ in range(rounds):
            for path in paths:path.write_text(str(round_))
            started=time.perf_counter();ctx.mounts.sync(timeout=30)
            latencies.append(time.perf_counter()-started)
        service=get_service()
        return dict(registrations=count,io_workers=len(service.workers),poll_interval=service.poll_interval,
                    attach_seconds=attach,sync_median_seconds=statistics.median(latencies),
                    sync_max_seconds=max(latencies),estimated_polls_per_second=count/service.poll_interval)


if __name__=='__main__':print(json.dumps(benchmark(),indent=2))
