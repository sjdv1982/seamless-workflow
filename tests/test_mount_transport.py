"""Fault-injected filesystem service tests, independent of Context ordering."""
from dataclasses import replace
from uuid import uuid4
import pytest
from seamless import Buffer
from seamless_workflow.attachments.fs.service import FileSystemService, FileSystem
from seamless_workflow.attachments.spec import AttachmentSpec
from seamless_workflow.attachments.session import Delivery, MountLease
from seamless_workflow.attachments.policy import ABSENT


def test_rewrite_during_read_and_precondition(tmp_path):
    path=tmp_path/'a.txt';path.write_text('before')
    class Faults(FileSystem):
        changed=False
        def read(self,p):
            data=super().read(p)
            if not self.changed:
                self.changed=True;path.write_text('after')
            return data
    service=FileSystemService(filesystem=Faults())
    events=[]
    try:
        reg=service.reserve(AttachmentSpec(path), 'text', uuid4().hex, lambda op,payload:events.append((op,payload)))
        initial=service.initial_read(reg).result(5)
        assert initial.buffer.content==b'after\n'
        buf=Buffer('ours','text');cs=buf.get_checksum();buf.tempref()
        lease=MountLease(cs,'test:delivery')
        delivery=Delivery(reg.session_id,1,cs.hex(),'text',initial.fingerprint,lease)
        initial.release()
        path.write_text('foreign')
        service.activate(reg)
        service.deliver(reg,delivery).result(5)
        assert events[0][1].outcome=='conflict'
        assert path.read_text()=='foreign'
        lease._release_refholds()
    finally:
        for _,event in events:
            if hasattr(event,'release'):event.release()
        service.close()


def test_replace_failure_keeps_target_and_removes_temporary(tmp_path):
    path=tmp_path/'a';path.write_bytes(b'old')
    class Faults(FileSystem):
        @staticmethod
        def replace(a,b): raise OSError('injected disk full')
    service=FileSystemService(filesystem=Faults())
    try:
        with pytest.raises(OSError):service._atomic_write(str(path),b'new',service._stat(path))
        assert path.read_bytes()==b'old'
        assert list(tmp_path.iterdir())==[path]
    finally:service.close()


def test_registry_prefix_conflict_and_swap(tmp_path):
    service=FileSystemService()
    try:
        first=service.reserve(AttachmentSpec(tmp_path/'tree',mode='w'),'deepfolder','1',lambda *a:None)
        with pytest.raises(ValueError):service.reserve(AttachmentSpec(tmp_path/'tree'/'leaf',mode='r'),'bytes','2',lambda *a:None)
        replacement=service.reserve(AttachmentSpec(tmp_path/'tree',mode='w'),'deepfolder','3',lambda *a:None,replaces=(first,))
        assert replacement.path==first.path
    finally:service.close()


def test_resolution_timeout_is_delivery_error(tmp_path, monkeypatch):
    import asyncio
    from seamless import Checksum
    async def blocked(self, celltype=None): await asyncio.Event().wait()
    service=FileSystemService();service.delivery_timeout=.02
    events=[]
    try:
        reg=service.reserve(AttachmentSpec(tmp_path/'a',mode='w'),'text','timeout',lambda op,p:events.append((op,p)))
        buf=Buffer('value','text');cs=buf.get_checksum();buf.tempref()
        lease=MountLease(cs,'test:timeout')
        monkeypatch.setattr(Checksum,'resolution',blocked)
        service.deliver(reg,Delivery(reg.session_id,1,cs.hex(),'text',None,lease)).result(2)
        assert events[0][1].outcome=='error' and 'TimeoutError' in events[0][1].reason
        lease._release_refholds()
    finally:service.close()


def test_native_notification_source(tmp_path):
    from seamless_workflow.attachments.fs.native import NativeNotifications
    from types import SimpleNamespace
    native=NativeNotifications()
    if native.fd < 0: pytest.skip('Native notifications unavailable on this platform')
    path=tmp_path/'a';reg=SimpleNamespace(path=str(path),active=True,closed=False)
    try:
        native.poll([reg])
        path.write_bytes(b'new')
        assert str(path) in native.poll([reg])
    finally:native.close()


def test_shared_read_only_poll_reads_once(tmp_path,monkeypatch):
    monkeypatch.setattr(FileSystemService,'poll_interval',100)
    class Counter(FileSystem):
        count=0
        def read(self,path):self.count+=1;return super().read(path)
    fs=Counter();service=FileSystemService(filesystem=fs);events=[]
    path=tmp_path/'a';path.write_text('a')
    try:
        regs=[service.reserve(AttachmentSpec(path,mode='r'),'text',str(n),lambda op,p:events.append((op,p))) for n in range(2)]
        for reg in regs:service.activate(reg)
        service.poll(regs[0],force=True).result(2)
        assert fs.count==1
        observations=[p for op,p in events if op=='_mount_observed']
        assert len(observations)==2 and observations[0].checksum==observations[1].checksum
    finally:
        for op,p in events:
            if hasattr(p,'release'):p.release()
        service.close()
