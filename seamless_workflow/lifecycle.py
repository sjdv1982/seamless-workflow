"""Process shutdown registration without keeping Contexts alive."""
from threading import RLock
from weakref import WeakSet
from seamless import register_close_hook

_contexts = WeakSet()
_lock = RLock()


def register(context):
    with _lock:
        _contexts.add(context)


def close_contexts():
    with _lock:
        contexts = tuple(_contexts)
    for context in contexts:
        context.close()


register_close_hook(close_contexts)
