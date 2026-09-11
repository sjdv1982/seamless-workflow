"""File-policy, real transport and controller interleaving acceptance tests."""
import asyncio
import os
import threading
from pathlib import Path
import pytest
from seamless import Cell, Buffer
from seamless_workflow import Context
from seamless_workflow.errors import AuthorityError, PathError
from seamless_workflow.attachments import AttachmentSpec, MountError, ConflictError
from seamless_workflow.attachments.policy import ABSENT, INVALID, decide_initial, classify_observation, detector
from seamless_workflow.attachments.manual import ManualDriver
from seamless_workflow.diagnostics import record_attachments


@pytest.mark.parametrize('mode,authority,disk,node,action', [
    ('rw','file-strict',ABSENT,'N','error'), ('rw','file',ABSENT,None,'nothing'),
    ('r','cell',ABSENT,'N','nothing'), ('rw','file',ABSENT,'N','write'),
    ('r','file','F',None,'sense'), ('rw','file','F','F','nothing'),
    ('rw','file','F','N','sense'), ('rw','cell','F',None,'sense'),
    ('r','cell','F','N','nothing'), ('w','cell','F','F','nothing'),
    ('rw','cell','F','N','write'), ('w','cell','F',None,'nothing'),
    ('rw','file',INVALID,'N','error'), ('rw','cell',INVALID,'N','write'),
    ('r','cell',INVALID,'N','nothing'), ('r','cell',INVALID,None,'error')])
def test_initial_policy(mode,authority,disk,node,action):
    assert decide_initial(AttachmentSpec('a', mode, authority),disk,node)==action


@pytest.mark.parametrize('celltype,content', [('text',b'abc\n\n'),('python',b'bad python !'),
    ('ipython',b'%magic\n'),('yaml',b'a: [broken'), ('plain', b'{ "a" : 1 }'),
    ('str',b'"hi"'),('int',b'12'),('float',b'1.2'),('bool',b'true'),
    ('bytes',b'\xff\x00'),('mixed',b'{"a":3}\n'),('binary',None)])
def test_canonical_idempotence(celltype,content):
    from seamless.checksum.canonical import canon_T
    if content is None:
        import numpy as np
        content=Buffer(np.array([1.,2.]),'binary').content
    canonical=canon_T(content,celltype)
    assert canon_T(canonical,celltype)==canonical


@pytest.mark.parametrize('mode', ['r','rw','w'])
def test_files(mode,tmp_path):
    p=tmp_path/'a.txt';p.write_text('file\n')
    with Context() as c:
        c.a=Cell('cell',celltype='text');c.a.mount(p,mode)
        assert c.a.value == ('cell' if mode=='w' else 'file')
        p.write_text('external\n')
        report=c.mounts.sync(timeout=5)
        assert c.a.value==('cell' if mode=='w' else 'external')
        c.a='user';c.mounts.sync(timeout=5)
        assert p.read_text()==('external\n' if mode=='r' else 'user\n')


def test_errors_recover_and_keep_graph_value(tmp_path):
    p=tmp_path/'a.json';p.write_text('broken')
    with Context() as c:
        c.a=Cell({'x':1},celltype='plain'); old=c.a.checksum.hex()
        c.b=c.a
        c.a.mount(p)
        assert isinstance(c.a.exception,MountError)
        assert c.b.block_reason=='blocked-by-error'
        assert c.get_graph()['nodes'][0]['value']['checksum']==old
        assert c.mounts.sync(timeout=5)[('a',)]['sense_error']
        p.write_text('{ "x": 2 }');c.mounts.sync(timeout=5)
        assert c.a.value=={'x':2} and c.b.value=={'x':2}
        assert p.read_text()=='{ "x": 2 }'
        p.write_text('bad');c.mounts.sync(timeout=5)
        c.a={'x':3};c.mounts.sync(timeout=5)
        assert c.a.exception is None
        p.write_text('bad');c.mounts.sync(timeout=5)
        del c.a.mount
        assert c.a.value=={'x':3}


def test_strict_missing_and_deletion(tmp_path):
    p=tmp_path/'a.txt'
    with Context() as c:
        c.a=Cell('old',celltype='text');c.a.mount(p,authority='file-strict')
        assert c.a.state=='failed'
        p.write_text('new');c.mounts.sync(timeout=5);assert c.a.value=='new'
        p.unlink();c.mounts.sync(timeout=5);assert c.a.state=='failed'


def test_configuration_and_graph(tmp_path):
    p=tmp_path/'a.txt'
    with Context() as c:
        c.a=Cell('old',celltype='text');c.b=Cell('b',celltype='text');c.a.mount(p)
        with pytest.raises(AuthorityError): c.a=c.b
        with pytest.raises(ValueError): c.a.celltype='bytes'
        with pytest.raises(ValueError): c.a=Cell('x',celltype='bytes')
        with pytest.raises(AttributeError): c.mounts=4
        graph=c.get_graph();c.set_graph(graph)
        assert c.a.mount.spec.path==str(p)
        c.set_graph(graph,mounts=False);assert c.a.mount.spec is None
        graph['__seamless_workflow__']='99.0'
        with pytest.raises(PathError): c.set_graph(graph)
        graph['__seamless_workflow__']='0.3';graph['nodes'][0]['mount']['unknown']=True
        with pytest.raises(PathError): c.set_graph(graph)


def test_stale_ack_order_and_foreign_revert():
    with Context() as c, record_attachments(c) as log:
        c.a=Cell('one',celltype='text')
        driver=ManualDriver().attach(c.a,'one')
        stale=driver.observation('old')
        c.a='two';delivery=driver.deliveries.popleft()
        driver.ack(delivery);c.get_graph()
        driver.registration.sink('_mount_observed',stale);c.get_graph()
        assert c.a.value=='two'
        driver.observe('foreign');c.get_graph();assert c.a.value=='foreign'
        driver.observe('two');c.get_graph();assert c.a.value=='two'
        classes=[dict(e[3])['classification'] for e in log.entries() if e[2]=='observation']
        assert classes==['stale','foreign','foreign']


def test_manual_latest_and_unmount():
    with Context() as c:
        c.a=Cell('a',celltype='text');d=ManualDriver().attach(c.a,'a')
        c.a='b';first=d.deliveries.popleft()
        c.a='c';c.a='d'
        d.ack(first);c.get_graph()
        latest=d.deliveries.popleft()
        assert latest.lease.checksum.resolve('text')=='d'
        del c.a.mount
        d.ack(latest);c.get_graph();assert c.a.value=='d'
        assert first.lease.released and latest.lease.released


def test_detector_and_reset(tmp_path):
    p=tmp_path/'a.txt'
    with Context() as c:
        c.a=Cell('ours',celltype='text');c.a.mount(p,mode='w')
        for n in range(3):
            p.write_text(f'theirs{n}');c.mounts.sync(timeout=5)
        assert isinstance(c.a.mount.error,ConflictError)
        assert c.a.mount.status['state']=='tripped'
        c.a.mount.clear_error();c.mounts.sync(timeout=5)
        assert p.read_text()=='ours\n'


def test_compression_symlink_persistence_and_close(tmp_path):
    import gzip
    target=tmp_path/'target.txt';target.write_text('old');target.chmod(0o640)
    link=tmp_path/'link.txt';link.symlink_to(target)
    with Context() as c:
        c.a=Cell('a',celltype='text');c.a.mount(link,authority='cell')
        c.a='final'
    assert link.is_symlink() and target.read_text()=='final\n'
    assert target.stat().st_mode & 0o777==0o640
    p=tmp_path/'data.gz'
    with Context() as c:
        c.a=Cell({'a':1},celltype='plain');c.a.mount(p,persistent=False)
        assert gzip.decompress(p.read_bytes())==Buffer({'a':1},'plain').content
    assert not p.exists()


def test_shared_read_and_overlap(tmp_path):
    p=tmp_path/'a.txt';p.write_text('a')
    with Context() as a, Context() as b:
        a.x=Cell(celltype='text');b.x=Cell(celltype='text')
        a.x.mount(p,mode='r');b.x.mount(p,mode='r')
        del b.x.mount
        with pytest.raises(ValueError): b.x.mount(p,mode='w')


def test_directory_and_async_sync(tmp_path):
    p=tmp_path/'folder';p.mkdir();(p/'a').write_bytes(b'first')
    with Context() as c:
        c.a=Cell(celltype='deepfolder');c.a.mount(p)
        assert c.a.value['a']==Buffer(b'first').get_checksum().hex()
        (p/'b').write_bytes(b'second')
        report=asyncio.run(c.mounts.synchronization(timeout=5))
        assert report[('a',)]['in_sync'] and 'b' in c.a.value


def test_sync_timeout_does_not_cancel_mount():
    with Context() as c:
        c.a=Cell('x',celltype='text');d=ManualDriver().attach(c.a,'x')
        with pytest.raises(TimeoutError): c.mounts.sync(timeout=.01)
        assert c.a.mount.spec is not None
        del c.a.mount


def test_mount_turns_do_not_materialize(tmp_path):
    from seamless.diagnostics import record_materialisation
    p=tmp_path/'a.json';p.write_text('{"a":1}')
    with Context() as c:
        c.a=Cell(celltype='plain')
        with record_materialisation() as log:
            c.a.mount(p)
            c.a={'b':2};c.mounts.sync(timeout=5)
            p.write_text('{"c":3}');c.mounts.sync(timeout=5)
        assert c._controller.ident not in log.threads


def test_failed_write_retries_and_recovers(tmp_path):
    from seamless_workflow.attachments.fs.service import get_service
    p=tmp_path/'missing'/'a.txt'
    with Context() as c:
        c.a=Cell('value',celltype='text');c.a.mount(p,mode='w')
        assert c.a.exception is None and isinstance(c.a.mount.error,MountError)
        assert c.mounts.sync(timeout=5)[('a',)]['error']
        p.parent.mkdir()
        # A new user value supersedes the retry and must attempt immediately.
        c.a='recovered';c.mounts.sync(timeout=5)
        assert c.a.mount.error is None and p.read_text()=='recovered\n'


def test_conditional_delete_preserves_foreign_edit(tmp_path):
    p=tmp_path/'a.txt'
    with Context() as c:
        c.a=Cell('ours',celltype='text');c.a.mount(p,persistent=False)
        p.write_text('foreign')
        del c.a.mount
        assert p.read_text()=='foreign'


def test_directory_delivery_and_cleanup(tmp_path):
    p=tmp_path/'folder';p.mkdir();(p/'old').write_bytes(b'old')
    leaf=Buffer(b'new');checksum=leaf.get_checksum();leaf.tempref()
    with Context() as c:
        c.a=Cell({'sub/a':checksum.hex()},celltype='deepfolder')
        c.a.mount(p,mode='w');c.mounts.sync(timeout=5)
        assert (p/'sub'/'a').read_bytes()==b'new'
        assert not (p/'old').exists()


def test_standalone_and_subpath_mounts():
    with pytest.raises(AttributeError): Cell().mount('x')
    with Context() as c:
        c.a={'a':1}
        with pytest.raises(AttributeError): c.a['a'].mount('x')


def test_empty_mount_sync():
    with Context() as c: assert c.mounts.sync(timeout=1)=={}


def test_refholder_audit_after_inflight_unmount():
    from seamless.reference_lifecycle import audit_reference_accounting
    with Context() as c:
        c.a=Cell('a',celltype='text');d=ManualDriver().attach(c.a,'a')
        c.a='b';delivery=d.deliveries.popleft()
        del c.a.mount
        d.ack(delivery);c.get_graph()
        audit_reference_accounting()
    audit_reference_accounting()


def test_malformed_internal_mount_messages_do_not_poison():
    with Context() as c:
        c._controller.call('_mount_delivered',None,klass=5)
        c._controller.call('_mount_cut',None,klass=5)
        c.a=1
        assert c.a.value==1


def test_widget_driver_round_trip_and_cleanup():
    from seamless_workflow.attachments.widget import WidgetDriver
    class Widget:
        def __init__(self):self._value=1;self.callbacks=[]
        def observe(self,callback,names):self.callbacks.append(callback)
        def unobserve(self,callback,names):self.callbacks.remove(callback)
        @property
        def value(self):return self._value
        @value.setter
        def value(self,value):
            self._value=value
            for callback in tuple(self.callbacks):callback({'new':value})
    widget=Widget()
    with Context() as c:
        c.a=Cell(0,celltype='int')
        driver=WidgetDriver(widget).attach(c.a)
        c.mounts.sync(timeout=5)
        assert c.a.value==1
        widget.value=2;c.mounts.sync(timeout=5);assert c.a.value==2
        c.a=3;c.mounts.sync(timeout=5);assert widget.value==3
        assert 'mount' not in c.get_graph()['nodes'][0]
        del c.a.mount
        assert widget.callbacks==[]


def test_directory_compressed_leaf_update(tmp_path):
    import gzip
    p=tmp_path/'tree';p.mkdir();(p/'a.gz').write_bytes(gzip.compress(b'old'))
    with Context() as c:
        c.a=Cell(celltype='deepfolder');c.a.mount(p)
        leaf=Buffer(b'new');cs=leaf.get_checksum();leaf.tempref()
        c.a={'a':cs.hex()};report=c.mounts.sync(timeout=5)
        assert report[('a',)]['in_sync']
        assert gzip.decompress((p/'a.gz').read_bytes())==b'new'
        assert not (p/'a').exists()


def test_directory_leaf_claims_survive_unmount(tmp_path):
    from seamless.reference_lifecycle import audit_reference_accounting
    from seamless.caching.buffer_cache import get_buffer_cache
    from seamless import Checksum
    p=tmp_path/'tree';p.mkdir();(p/'a').write_bytes(b'leaf-retention')
    with Context() as c:
        c.a=Cell(celltype='deepfolder');c.a.mount(p)
        checksum=Checksum(c.a.value['a'])
        del c.a.mount
        assert checksum.resolve().content==b'leaf-retention'
        audit_reference_accounting()
        del c.a
        audit_reference_accounting()


def test_graph_invalid_reservation_leaves_existing_mount(tmp_path):
    p=tmp_path/'a';q=tmp_path/'b'
    with Context() as a, Context() as b:
        a.x=Cell('a',celltype='text');a.x.mount(p)
        b.x=Cell('b',celltype='text');b.x.mount(q)
        graph=a.get_graph();graph['nodes'][0]['mount']['path']=str(q)
        with pytest.raises(ValueError):a.set_graph(graph)
        assert a.x.mount.spec.path==str(p)
        a.x='still active';a.mounts.sync(timeout=5)
        assert p.read_text()=='still active\n'


@pytest.mark.parametrize('celltype', ['plain','str','int','float','bool','binary','mixed'])
def test_invalid_canonical_input(celltype):
    from seamless.checksum.canonical import canon_T
    with pytest.raises((ValueError,TypeError)):canon_T(b'\xffinvalid',celltype)


def test_zstandard_roundtrip(tmp_path):
    zstandard=pytest.importorskip('zstandard')
    p=tmp_path/'a.zst'
    with Context() as c:
        c.a=Cell('hello',celltype='text');c.a.mount(p)
        assert zstandard.ZstdDecompressor().decompress(p.read_bytes())==b'hello\n'
        p.write_bytes(zstandard.ZstdCompressor().compress(b'updated'))
        assert c.mounts.sync(timeout=5)[('a',)]['in_sync']
        assert c.a.value=='updated'


def test_delete_mounted_node_with_downstream(tmp_path):
    p=tmp_path/'a'
    with Context() as c:
        c.a=Cell('a',celltype='text');c.a.mount(p,persistent=False)
        c.b=c.a
        del c.a
        c.compute(timeout=5)
        c.a=Cell('new',celltype='text');c.a.mount(p)
        c.mounts.sync(timeout=5)
        assert c.a.mount.status['state']=='active'
