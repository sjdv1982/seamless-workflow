"""Optional Linux notifications. Polling remains the correctness fallback.

Only directory watches are installed, so editor-style target replacement does
not invalidate a watch. This source only marks registrations dirty; the normal
transport still performs stable reads and owns watch sequences.
"""
import os
import sys
import struct


class NativeNotifications:
    def __init__(self):
        self.fd = -1
        self.watches = {}
        if not sys.platform.startswith('linux'): return
        try:
            import ctypes
            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.inotify_init1.argtypes = [ctypes.c_int]
            self.libc.inotify_init1.restype = ctypes.c_int
            self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            self.libc.inotify_add_watch.restype = ctypes.c_int
            self.libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
            self.libc.inotify_rm_watch.restype = ctypes.c_int
            self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        except (AttributeError, OSError): pass

    def poll(self, registrations):
        if self.fd < 0: return set()
        parents = {os.path.dirname(r.path) for r in registrations if r.active and not r.closed}
        for path in set(self.watches) - parents:
            self.libc.inotify_rm_watch(self.fd, self.watches.pop(path))
        for path in parents - set(self.watches):
            # MODIFY, ATTRIB, CLOSE_WRITE, MOVED_FROM/TO, CREATE, DELETE,
            # DELETE_SELF and MOVE_SELF. Failed watches fall back to polling.
            wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), 0x00000FCE)
            if wd >= 0: self.watches[path] = wd
        reverse = {wd: path for path, wd in self.watches.items()}
        changed = set()
        while True:
            try: data = os.read(self.fd, 65536)
            except BlockingIOError: break
            except OSError: break
            if not data: break
            offset = 0
            while offset + 16 <= len(data):
                wd, mask, cookie, length = struct.unpack_from('iIII', data, offset)
                name = os.fsdecode(data[offset+16:offset+16+length].split(b'\0', 1)[0])
                offset += 16 + length
                if mask & 0x00004000:  # queue overflow: request every path
                    return {r.path for r in registrations}
                parent = reverse.get(wd)
                if parent is None: continue
                if mask & 0x00008000:
                    self.watches.pop(parent, None)
                changed.add(os.path.join(parent, name) if name else parent)
        return changed

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
