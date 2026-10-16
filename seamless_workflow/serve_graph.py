"""Run a saved Seamless workflow graph as a long-lived process."""

import argparse
import json
import os
import signal
import threading

import seamless
import seamless.config
from seamless_workflow import Context


def _wait_for_shutdown():
    """Keep the process alive until SIGINT or SIGTERM requests shutdown."""
    shutdown = threading.Event()
    previous_handlers = {}

    def request_shutdown(_signum, _frame):
        shutdown.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_shutdown)
        shutdown.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Serve a Seamless workflow graph until shutdown."
    )
    parser.add_argument("graph", help="path to the workflow graph JSON file")
    args = parser.parse_args(argv)

    try:
        with open(args.graph, encoding="utf-8") as graph_file:
            graph = json.load(graph_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        parser.error(f"cannot load workflow graph {args.graph!r}: {exc}")

    try:
        seamless.config.init(workdir=os.getcwd())
        with Context() as ctx:
            # set_graph loads the serialized graph and starts reactive derivation.
            ctx.set_graph(graph, mounts=False, shares=True)
            _wait_for_shutdown()
    finally:
        seamless.close()


if __name__ == "__main__":
    main()
