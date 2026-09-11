"""Filesystem transport. No graph or Context objects cross this boundary.

Each registration has a serial operation queue. The broker submits polls; the
bounded daemon pool performs all filesystem operations, including stat walks.
"""
from __future__ import annotations
import gzip
import os
import stat
import time
import secrets
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from threading import RLock, Thread, Event

from seamless import Buffer, Checksum
from seamless.checksum.canonical import canon_T, DIRECTORY_CELLTYPES
from ..policy import ABSENT, INVALID
from ..session import Observation, DeliveryAck, MountLease


class FileSystem:
    """Injectable filesystem boundary for deterministic race/failure tests."""
    stat = staticmethod(os.stat)
    read = staticmethod(lambda path: Path(path).read_bytes())
    replace = staticmethod(os.replace)
    unlink = staticmethod(os.unlink)


@dataclass(eq=False)
class Registration:
    path: str
    session_id: str
    spec: object
    celltype: str
    sink: object
    active: bool = False
    closed: bool = False
    ws: int = 0
    baseline: object = None
    lock: object = field(default_factory=RLock)
    queue: object = field(default_factory=deque)
    busy: bool = False
    poll_queued: bool = False
    closing: bool = False
    replaces: tuple = ()

    @property
    def directory(self): return self.celltype in DIRECTORY_CELLTYPES


class FileSystemService:
    poll_interval = .2
    racy_window = 2.
    delivery_timeout = 60.
    max_file_size = 1024**3
    max_files = 100_000
    max_tree_size = 10 * 1024**3
    scan_timeout = 60.

    def __init__(self, *, filesystem=None, workers=4, native=False):
        self.fs = filesystem or FileSystem()
        from .native import NativeNotifications
        self.native = NativeNotifications() if native else None
        self.lock = RLock()
        self.registrations = set()
        self.read_locks = {}
        self.work = Queue()
        self.stopping = Event()
        self.workers = [Thread(target=self._worker, name=f'Mount-IO-{n}', daemon=True) for n in range(workers)]
        for worker in self.workers: worker.start()
        self.broker = Thread(target=self._broker, name='Mount-broker', daemon=True)
        self.broker.start()

    def _worker(self):
        while True:
            task = self.work.get()
            if task is None: return
            reg, function, future = task
            try:
                with reg.io_lock:
                    result = function()
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                with reg.lock:
                    if reg.queue:
                        self.work.put((reg, *reg.queue.popleft()))
                    else: reg.busy = False

    def _submit(self, reg, function):
        future = Future()
        with reg.lock:
            if reg.busy: reg.queue.append((function, future))
            else:
                reg.busy = True
                self.work.put((reg, function, future))
        return future

    def reserve(self, spec, celltype, session_id, sink, *, replaces=()):
        path = os.path.realpath(os.path.abspath(spec.path))
        reg = Registration(path, session_id, spec, celltype, sink, replaces=tuple(replaces))
        reg.service = self
        with self.lock:
            reg.io_lock = self.read_locks.setdefault(path, RLock())
            if self.stopping.is_set(): raise RuntimeError('Filesystem service closed')
            for other in self.registrations:
                if other.closed or other.closing or other in replaces: continue
                overlap = path == other.path or path.startswith(other.path + os.sep) or other.path.startswith(path + os.sep)
                cleanup = reg.directory and not spec.persistent
                other_cleanup = other.directory and not other.spec.persistent
                if overlap and ('w' in spec.mode or 'w' in other.spec.mode or cleanup or other_cleanup):
                    raise ValueError(f'Mount path overlaps existing registration: {path!r}, {other.path!r}')
            self.registrations.add(reg)
        return reg

    def activate(self, reg):
        reg.active = True

    def initial_read(self, reg):
        def initial():
            reg.ws += 1
            observation = self._read(reg, reg.ws)
            reg.baseline = observation.fingerprint
            return observation
        return self._submit(reg, initial)

    @staticmethod
    def _temporary(name):
        return name.startswith('.') and '.seamless-' in name and name.endswith('.tmp')

    def _stat(self, path):
        try:
            s = self.fs.stat(path)
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        except FileNotFoundError: return None

    def _fingerprint(self, reg):
        root = self._stat(reg.path)
        if root is None or not reg.directory: return root
        if not stat.S_ISDIR(self.fs.stat(reg.path).st_mode): raise NotADirectoryError(reg.path)
        start, entries = time.monotonic(), []
        for parent, dirs, files in os.walk(reg.path, followlinks=False):
            dirs.sort()
            for name in sorted(files):
                if self._temporary(name): continue
                path = os.path.join(parent, name)
                if os.path.islink(path):
                    raise ValueError('Directory mounts do not follow leaf symlinks')
                entries.append((os.path.relpath(path, reg.path), self._stat(path)))
                if len(entries) > self.max_files or time.monotonic() - start > self.scan_timeout:
                    raise ValueError('Directory scan limit exceeded')
        return root, tuple(entries)

    def _decode(self, path, data):
        if path.endswith('.gz'):
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                return stream.read(self.max_file_size + 1)
        if path.endswith('.zst'):
            import zstandard
            with zstandard.ZstdDecompressor().stream_reader(data) as stream:
                return stream.read(self.max_file_size + 1)
        return data

    def _encode(self, path, data):
        if path.endswith('.gz'): return gzip.compress(data, compresslevel=6, mtime=0)
        if path.endswith('.zst'):
            import zstandard
            return zstandard.ZstdCompressor(level=3).compress(data)
        return data

    def _read_bytes(self, path):
        if self.fs.stat(path).st_size > self.max_file_size: raise ValueError('File size limit exceeded')
        content = self._decode(path, self.fs.read(path))
        if len(content) > self.max_file_size: raise ValueError('Decompressed file size limit exceeded')
        return content

    def _read(self, reg, ws):
        leases = []
        fingerprint = None
        try:
            for attempt in range(5):
                for lease in leases: lease._release_refholds()
                leases = []
                fingerprint = self._fingerprint(reg)
                if fingerprint is None: return Observation(reg.session_id, ws, None, ABSENT)
                if reg.directory:
                    index, size = {}, 0
                    started = time.monotonic()
                    for relative, _ in fingerprint[1]:
                        content = self._read_bytes(os.path.join(reg.path, relative))
                        size += len(content)
                        if size > self.max_tree_size or time.monotonic() - started > self.scan_timeout:
                            raise ValueError('Directory content limit exceeded')
                        key = relative[:-3] if relative.endswith('.gz') else relative[:-4] if relative.endswith('.zst') else relative
                        if key in index: raise ValueError(f'Duplicate decompressed directory path: {key}')
                        buf = Buffer(content)
                        cs = buf.get_checksum()
                        buf.tempref()
                        leases.append(MountLease(cs, f'mount:{reg.session_id}:leaf'))
                        index[key] = cs.hex()
                    buf = Buffer(index, 'plain')
                else:
                    buf = Buffer(canon_T(self._read_bytes(reg.path), reg.celltype))
                after = self._fingerprint(reg)
                if fingerprint == after: break
                time.sleep(.01 * (attempt + 1))
            else: raise OSError('File changed throughout stable read')
            cs = buf.get_checksum()
            buf.tempref()
            leases.insert(0, MountLease(cs, f'mount:{reg.session_id}:observation'))
            return Observation(reg.session_id, ws, fingerprint, cs.hex(), buf, tuple(leases))
        except Exception as exc:
            for lease in leases: lease._release_refholds()
            return Observation(reg.session_id, ws, fingerprint, INVALID, reason=f'{type(exc).__name__}: {exc}')

    def _send(self, reg, operation, payload):
        if reg.closed and operation == '_mount_observed':
            payload.release()
            return
        reg.sink(operation, payload)

    def _poll(self, reg, *, force=False):
        with reg.io_lock: return self._poll_locked(reg, force=force)

    def _poll_locked(self, reg, *, force=False):
        if reg.closed or not reg.active: return
        try:
            fingerprint = self._fingerprint(reg)
            root = fingerprint[0] if reg.directory and fingerprint else fingerprint
            stamps = [root] if not reg.directory or not fingerprint else [root, *(fp for _, fp in fingerprint[1])]
            racy = any(fp is not None and abs(time.time() - fp[3] / 1e9) <= self.racy_window for fp in stamps)
        except Exception:
            fingerprint, racy = INVALID, True
        if force or fingerprint != reg.baseline or racy:
            reg.ws += 1
            reg.baseline = fingerprint
            observation = self._read(reg, reg.ws)
            if reg.spec.mode == 'r':
                with self.lock:
                    peers = tuple(r for r in self.registrations if r is not reg and r.active and not r.closed
                                  and r.spec.mode == 'r' and r.path == reg.path and r.celltype == reg.celltype)
                for peer in peers:
                    with peer.lock:
                        peer.ws += 1
                        peer.baseline = fingerprint
                        leases = tuple(MountLease(lease.checksum, f'mount:{peer.session_id}:shared-read')
                                       for lease in observation.leases)
                        shared = Observation(peer.session_id, peer.ws, observation.fingerprint,
                                             observation.checksum, observation.buffer, leases, observation.reason)
                        self._send(peer, '_mount_observed', shared)
            self._send(reg, '_mount_observed', observation)

    def poll(self, reg, *, force=False):
        return self._submit(reg, lambda: self._poll(reg, force=force))

    def request_cut(self, reg, cut_id):
        def cut():
            self._poll(reg, force=True)
            self._send(reg, '_mount_cut', (reg.session_id, cut_id, reg.ws))
        return self._submit(reg, cut)

    def _broker(self):
        while not self.stopping.wait(self.poll_interval):
            with self.lock: registrations = tuple(self.registrations)
            changed = self.native.poll(registrations) if self.native else set()
            shared = set()
            for reg in registrations:
                if reg.spec.mode == "r":
                    key = (reg.path, reg.celltype)
                    if key in shared: continue
                    if reg.active and not reg.closed: shared.add(key)
                with reg.lock:
                    if reg.closed or not reg.active or reg.poll_queued: continue
                    reg.poll_queued = True
                def poll(reg=reg, force=reg.path in changed):
                    try:
                        self._poll(reg, force=force)
                        self._send(reg, '_mount_tick', reg.session_id)
                    finally: reg.poll_queued = False
                self._submit(reg, poll)

    def _atomic_write(self, path, content, expected):
        parent, name = os.path.split(path)
        temporary = os.path.join(parent, f'.{name}.seamless-{secrets.token_hex(8)}.tmp')
        try:
            with open(temporary, 'xb') as handle:
                handle.write(self._encode(path, content))
            if expected is not None:
                os.chmod(temporary, stat.S_IMODE(self.fs.stat(path).st_mode))
            if self._stat(path) != expected: return False
            self.fs.replace(temporary, path)
            return True
        finally:
            try: self.fs.unlink(temporary)
            except FileNotFoundError: pass

    def _resolve(self, checksum):
        import asyncio
        async def resolve():
            return await asyncio.wait_for(checksum.resolution(), self.delivery_timeout)
        return asyncio.run(resolve())

    def _write_directory(self, reg, delivery, content):
        import json
        index = json.loads(content)
        if not isinstance(index, dict): raise ValueError('Directory index must be a mapping')
        if len(index) > self.max_files: raise ValueError('Directory file-count limit exceeded')
        for name in index:
            if (not isinstance(name, str) or not name or os.path.isabs(name)
                    or any(part in {'', '.', '..'} for part in name.split('/'))):
                raise ValueError(f'Unsafe directory path: {name!r}')
        if self._fingerprint(reg) != delivery.expected_fingerprint: return False
        physical_names = {}
        if delivery.expected_fingerprint is not None:
            for relative, _ in delivery.expected_fingerprint[1]:
                key = relative[:-3] if relative.endswith('.gz') else relative[:-4] if relative.endswith('.zst') else relative
                if key in physical_names: raise ValueError(f'Duplicate decompressed directory path: {key}')
                physical_names[key] = relative
        os.makedirs(reg.path, exist_ok=True)
        total, started = 0, time.monotonic()
        for name, checksum in sorted(index.items()):
            if reg.closed: raise RuntimeError('Directory mount closed during write')
            default_name = name + ('.gz' if name.endswith('.gz') else '.zst' if name.endswith('.zst') else '')
            path = os.path.join(reg.path, physical_names.get(name, default_name))
            if os.path.commonpath((os.path.realpath(path), reg.path)) != reg.path:
                raise ValueError('Directory path escapes mount through a symlink')
            buf = self._resolve(Checksum(checksum))
            data = buf.content if hasattr(buf, 'content') else bytes(buf)
            total += len(data)
            if total > self.max_tree_size or time.monotonic() - started > self.scan_timeout:
                raise ValueError('Directory delivery limit exceeded')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            expected = self._stat(path)
            if expected is not None and self._read_bytes(path) == data: continue
            if not self._atomic_write(path, data, expected): raise OSError(f'Directory leaf changed: {name}')
        if reg.spec.mode == 'w' or reg.spec.authority == 'cell':
            for parent, dirs, files in os.walk(reg.path, topdown=False):
                for name in files:
                    path = os.path.join(parent, name)
                    if self._temporary(name): continue
                    relative = os.path.relpath(path, reg.path)
                    key = relative[:-3] if relative.endswith('.gz') else relative[:-4] if relative.endswith('.zst') else relative
                    if key not in index: self.fs.unlink(path)
                for name in dirs:
                    try: os.rmdir(os.path.join(parent, name))
                    except OSError: pass
        return True

    def deliver(self, reg, delivery):
        # The controller may release its delivery claim on unmount while this
        # queued operation is still resolving. Retain a separate worker claim.
        transport_lease = MountLease(delivery.lease.checksum, f'mount:{reg.session_id}:io:{delivery.seq}')
        def write():
            try:
                if reg.closed: raise RuntimeError('Mount was unregistered')
                buf = self._resolve(transport_lease.checksum)
                content = buf.content if hasattr(buf, 'content') else bytes(buf)
                written = (self._write_directory(reg, delivery, content) if reg.directory else
                           self._atomic_write(reg.path, content, delivery.expected_fingerprint))
                if not written:
                    ack = DeliveryAck(reg.session_id, delivery.seq, reg.ws, 'conflict')
                else:
                    reg.ws += 1
                    reg.baseline = self._fingerprint(reg)
                    ack = DeliveryAck(reg.session_id, delivery.seq, reg.ws, 'written', delivery.checksum, reg.baseline)
            except Exception as exc:
                ack = DeliveryAck(reg.session_id, delivery.seq, reg.ws, 'error', reason=f'{type(exc).__name__}: {exc}')
            transport_lease._release_refholds()
            self._send(reg, '_mount_delivered', ack)
            if ack.outcome == 'conflict': self._poll(reg, force=True)
        return self._submit(reg, write)

    def unregister(self, reg, *, delete=False, expected=None):
        reg.active = False
        reg.closing = True
        def cleanup():
            try:
                with self.lock:
                    remounted = any(r is not reg and not r.closed and not r.closing and r.path == reg.path
                                    for r in self.registrations)
                if delete and not remounted and self._fingerprint(reg) == expected and expected is not None:
                    if reg.directory:
                        import shutil
                        shutil.rmtree(reg.path)
                    else: self.fs.unlink(reg.path)
            finally:
                reg.closed = True
                with self.lock:
                    self.registrations.discard(reg)
                    if not any(r.path == reg.path for r in self.registrations):
                        self.read_locks.pop(reg.path, None)
        return self._submit(reg, cleanup)

    def close(self, timeout=60):
        self.stopping.set()
        self.broker.join(timeout)
        if self.native is not None: self.native.close()
        with self.lock: regs = tuple(self.registrations)
        deadline = time.monotonic() + timeout
        for reg in regs:
            try: self.unregister(reg).result(max(0, deadline - time.monotonic()))
            except Exception: pass
        for _ in self.workers: self.work.put(None)
        for worker in self.workers: worker.join(max(0, deadline - time.monotonic()))


_service = None
_service_lock = RLock()


def get_service():
    global _service
    with _service_lock:
        if _service is None or _service.stopping.is_set(): _service = FileSystemService(native=os.environ.get("SEAMLESS_MOUNT_NATIVE") == "1")
        return _service


def close_service():
    global _service
    if _service is not None: _service.close()
