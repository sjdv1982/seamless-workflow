"""seamless-client.js contract in Node, using native fetch/WebSocket plus fakes.

Only Node-dependent cases skip when Node is unavailable. Packaging runs without
external downloads. Tests are independent of Context and drive real ShareServer.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

from test_share_transport import Spec, attach, deliver
from test_contract_shares import get


ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "seamless_workflow/attachments/share/static/seamless-client.js"
NODE = shutil.which("node")


PRELUDE = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const realSetTimeout = global.setTimeout;
const wait = async (predicate, message='condition timed out') => {
  const deadline = Date.now() + 5000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(message);
    await new Promise(resolve => realSetTimeout(resolve, 5));
  }
};
global.window = {location: {origin: 'http://example.test:5813', protocol: 'http:', hostname: 'example.test', host: 'example.test:5813', port: '5813', href: 'http://example.test:5813/page.html'}};
global.location = window.location;
window.WebSocket=global.WebSocket; window.fetch=global.fetch; window.Blob=global.Blob;
const loadClient = () => vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'), {filename: process.argv[2]});
'''

FAKES = r'''
const requests = [];
const sockets = [];
const warnings = [];
console.warn = (...args) => warnings.push(args.join(' '));
class FakeWebSocket {
  static CONNECTING=0; static OPEN=1; static CLOSING=2; static CLOSED=3;
  constructor(url) {
    this.url=String(url); this.readyState=0; this.listeners={}; sockets.push(this);
    queueMicrotask(() => { this.readyState=1; this.dispatch('open', {}); });
  }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  removeEventListener(type, callback) { this.listeners[type]=(this.listeners[type]||[]).filter(fn=>fn!==callback); }
  dispatch(type, event) {
    if (this['on'+type]) this['on'+type](event);
    for (const listener of this.listeners[type]||[]) listener(event);
  }
  send() {}
  close() { this.readyState=3; this.dispatch('close', {code:1006}); }
}
global.WebSocket=FakeWebSocket;
window.WebSocket=FakeWebSocket;
const response = (body='1\n', status=200, headers={}) => ({
  ok: status>=200 && status<300, status,
  headers: {get: name => ({'content-type':'application/json','etag':'"'+('1'.repeat(64))+'"','x-seamless-marker':'1',...headers})[name.toLowerCase()] ?? null},
  text: async()=>body, blob: async()=>new Blob([body]),
  json: async()=>typeof body==='string'?JSON.parse(body):body,
  arrayBuffer: async()=>new TextEncoder().encode(body).buffer
});
let fetchImpl = async (url, options) => response();
global.fetch=window.fetch=async (url, options={}) => {
  requests.push({url:String(url), options});
  return fetchImpl(String(url), options);
};
const message = (socket, value) => socket.dispatch('message', {data:JSON.stringify(value)});
const share = (key='a', checksum='1'.repeat(64), marker=1, extra={}) => ({url:'/ctx/'+key, readonly:false, content_type:'application/json', binary:false, checksum, marker, ...extra});
const snapshot = (socket, entries) => {
  message(socket, ['Seamless share update server','1.0']);
  message(socket, ['shares',entries]);
};
const puts = () => requests.filter(r=>(r.options.method||'GET').toUpperCase()==='PUT');
'''


def run_js(script, fake=True):
    complete = PRELUDE + (FAKES if fake else "") + "\n(async () => {\n" + script + "\n})().then(()=>process.exit(0), error=>{console.error(error);process.exit(1)});\n"
    result = subprocess.run([NODE, "-", str(CLIENT)], input=complete, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


class TestShareClientJS:
    pytestmark = pytest.mark.skipif(NODE is None, reason="Node is unavailable")

    def test_default_origin_port_url_aliases_and_conflict_warning(self):
        """seamless-client.js: one origin, three arguments, URL/port aliases."""
        run_js(r'''
loadClient();
const ctx=connect_seamless();
await wait(()=>sockets.length===1);
assert.equal(sockets[0].url, 'ws://example.test:5813/ctx');
assert.equal(ctx.self.share_namespace, 'ctx');
assert.equal(ctx.self.server.replace(/\/$/,''), 'http://example.test:5813');
connect_seamless(6000, null, 'port');
await wait(()=>sockets.length===2);
assert.equal(sockets[1].url, 'ws://example.test:6000/port');
connect_seamless('http://first.test:1234', 'https://second.test:4567', 'chosen');
await wait(()=>sockets.length===3);
assert.equal(sockets[2].url, 'wss://second.test:4567/chosen');
assert.ok(warnings.length>0);
''')

    def test_metadata_key_mapping_handlers_and_list_changes(self):
        """seamless-client.js: entry/self metadata; handlers survive shares messages."""
        run_js(r'''
loadClient();
const ctx=connect_seamless();
let lists=0;
ctx.self.onsharelist=()=>lists++;
await wait(()=>sockets.length===1);
snapshot(sockets[0], {'sub/a':share('sub/a'), 'index.html':share('index.html')});
await wait(()=>ctx.sub__a && ctx['index.html']);
const entry=ctx.sub__a, handler=()=>{};
entry.onchange=handler;
assert.equal(entry.auto_read,true);
assert.equal(ctx['index.html'].auto_read,false);
for(const key of ['value','checksum','marker','binary','content_type','readonly','auto_read','set','oninput','onchange']) assert.ok(key in entry,key);
for(const key of ['sharelist','onsharelist','oninput','onchange','get_value','connect','ws','server','share_namespace']) assert.ok(key in ctx.self,key);
message(sockets[0], ['shares',{'sub/a':share('sub/a'), b:share('b')}]);
await wait(()=>ctx.b && !ctx['index.html']);
assert.equal(ctx.sub__a,entry);
assert.equal(entry.onchange,handler);
assert.ok(lists>=2);
assert.deepEqual([...ctx.self.sharelist].sort(), ['b','sub/a']);
''')

    def test_text_binary_null_and_dot_auto_read(self):
        """seamless-client.js: text/Blob/null values and opt-out auto-read assets."""
        run_js(r'''
fetchImpl=async url=>url.endsWith('/binary')?response('bytes',200,{'content-type':'application/octet-stream'}):url.endsWith('/nil')?response('',204):response('text');
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0], {a:share('a'),binary:share('binary','2'.repeat(64),1,{binary:true,content_type:'application/octet-stream'}),nil:share('nil','3'.repeat(64)), 'image.png':share('image.png')});
await wait(()=>ctx.a?.value==='text' && ctx.binary?.value instanceof Blob && ctx.nil?.value===null);
assert.equal(await ctx.binary.value.text(),'bytes');
assert.ok(!requests.some(r=>r.url.includes('image.png')));
message(sockets[0], ['update',['image.png','4'.repeat(64),2]]);
await new Promise(resolve=>realSetTimeout(resolve,25));
assert.ok(!requests.some(r=>r.url.includes('image.png')));
''')

    def test_one_put_at_a_time_latest_pending_and_raw_cas_body(self):
        """seamless-client.js: raw PUT, one flight per key, latest pending and base marker."""
        run_js(r'''
const pending=[];
fetchImpl=async(url,options)=>options.method==='PUT'?await new Promise(resolve=>pending.push(resolve)):response('1\n');
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>ctx.a?.value==='1\n');
ctx.a.set('2');await wait(()=>puts().length===1);
ctx.a.set('3');ctx.a.set('4');
assert.equal(puts().length,1);
assert.equal(puts()[0].options.body,'2');
assert.equal(new URL(puts()[0].url).searchParams.get('marker'),'1');
pending.shift()(response(JSON.stringify({checksum:'2'.repeat(64),marker:2})));
await wait(()=>puts().length===2);
assert.equal(puts()[1].options.body,'4');
assert.equal(new URL(puts()[1].url).searchParams.get('marker'),'2');
pending.shift()(response(JSON.stringify({checksum:'4'.repeat(64),marker:3})));
await wait(()=>ctx.a.marker===3);
assert.equal(puts().length,2);
''')

    def test_409_drops_local_change_and_fetches_server_value(self):
        """seamless-client.js: conflict drops the local write and refreshes server state."""
        run_js(r'''
let current='1\n';
fetchImpl=async(url,options)=>options.method==='PUT'?(current='9\n',response(JSON.stringify({checksum:'9'.repeat(64),marker:9}),409)):response(current,200,{'x-seamless-marker':current==='9\n'?'9':'1','etag':'"'+((current==='9\n'?'9':'1').repeat(64))+'"'});
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>ctx.a?.value==='1\n');
ctx.a.set('2');await wait(()=>ctx.a.value==='9\n');
assert.equal(puts().length,1);assert.equal(ctx.a.marker,9);
assert.equal(ctx.a.checksum,'9'.repeat(64));
''')

    def test_reconnect_backoff_and_adopts_server_marker(self):
        """seamless-client.js: reconnect uses backoff and adopts restarted server markers."""
        run_js(r'''
const timers=[];
global.setTimeout=(fn,delay,...args)=>{timers.push({fn,delay,args});return timers.length;};
global.clearTimeout=()=>{};
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a','7'.repeat(64),70)});await wait(()=>ctx.a?.marker===70);
sockets[0].close();await wait(()=>timers.length>0);
const first=timers.shift();assert.ok(first.delay>0);first.fn(...first.args);
await wait(()=>sockets.length===2);
sockets[1].close();await wait(()=>timers.length>0);
const second=timers.shift();assert.ok(second.delay>=first.delay);second.fn(...second.args);
await wait(()=>sockets.length===3);
snapshot(sockets[2],{a:share('a','1'.repeat(64),1)});await wait(()=>ctx.a.marker===1);
assert.equal(ctx.a.checksum,'1'.repeat(64));
''')

    def test_stale_get_cannot_overwrite_newer_read(self):
        """seamless-client.js; Record: delayed GET cannot replace a newer announced value."""
        run_js(r'''
const reads=[];
fetchImpl=async(url,options)=>await new Promise(resolve=>reads.push(resolve));
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>reads.length===1);
message(sockets[0],['update',['a','2'.repeat(64),2]]);
await wait(()=>reads.length===2);
reads[1](response('new',200,{'etag':'"'+('2'.repeat(64))+'"','x-seamless-marker':'2'}));
await wait(()=>ctx.a.value==='new');
reads[0](response('old'));
await new Promise(resolve=>realSetTimeout(resolve,25));
assert.equal(ctx.a.value,'new');assert.equal(ctx.a.marker,2);
''')

    def test_real_server_text_put_and_change_callback(self):
        """seamless-client.js: native Node fetch/WebSocket against the real one-port server."""
        from seamless_workflow.attachments.share.server import ShareServer
        server=ShareServer(host='127.0.0.1',port=0)
        owner=object()
        try:
            reg,sink,url=attach(server,owner=owner)
            deliver(server,reg,sink)
            run_js('''
loadClient();
const ctx=connect_seamless('''+json.dumps(server.url)+r''',null,'ctx');
await wait(()=>ctx.a?.value?.trim()==='1');
let changes=0;ctx.a.onchange=()=>changes++;
ctx.a.set('2');
await wait(()=>ctx.a.value?.trim()==='2' && ctx.a.marker>=2);
assert.ok(ctx.a.marker>=2);assert.ok(changes>0);
assert.equal(ctx.a.content_type.split(';')[0],'application/json');
assert.equal(ctx.a.readonly,false);
''',fake=False)
            assert get(url).json()==2
        finally:
            server.close()


def test_wheel_contains_client_and_optional_share_extra(tmp_path):
    """Packaging: share extra supplies aiohttp and wheel includes static browser client."""
    config=tomllib.loads((ROOT/'pyproject.toml').read_text())
    extra=config['project']['optional-dependencies']['share']
    assert any(requirement.lower().startswith('aiohttp') for requirement in extra)
    assert not any(requirement.lower().startswith('aiohttp') for requirement in config['project']['dependencies'])
    isolated=tmp_path/'source'
    isolated.mkdir()
    for name in ('pyproject.toml','README.md'):
        shutil.copy2(ROOT/name,isolated/name)
    shutil.copytree(ROOT/'seamless_workflow',isolated/'seamless_workflow',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    output=tmp_path/'wheel'
    result=subprocess.run([sys.executable,'-m','pip','wheel','--no-deps','--no-build-isolation','--wheel-dir',str(output),str(isolated)],capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    wheel=next(output.glob('*.whl'))
    with zipfile.ZipFile(wheel) as archive:
        path='seamless_workflow/attachments/share/static/seamless-client.js'
        assert archive.read(path)==CLIENT.read_bytes()
        metadata=archive.read(next(name for name in archive.namelist() if name.endswith('.dist-info/METADATA'))).decode()
        assert 'Provides-Extra: share' in metadata
        assert any('aiohttp' in line.lower() and 'share' in line for line in metadata.splitlines() if line.startswith('Requires-Dist:'))


class TestShareClientJSRaces:
    pytestmark = pytest.mark.skipif(NODE is None, reason="Node is unavailable")

    def test_failed_get_preserves_last_value_and_announced_metadata(self):
        """seamless-client.js; Reading: unsuccessful refresh preserves the last value."""
        run_js(r'''
let failed=false;
fetchImpl=async()=>failed?response('unavailable',503):response('last',200,{'x-seamless-marker':'1'});
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>ctx.a?.value==='last');
let changes=0;ctx.a.onchange=()=>changes++;failed=true;
message(sockets[0],['update',['a','2'.repeat(64),2]]);
await wait(()=>requests.length>=2);await new Promise(resolve=>realSetTimeout(resolve,25));
assert.equal(ctx.a.value,'last');assert.equal(changes,0);
assert.equal(ctx.a.marker,2);assert.equal(ctx.a.checksum,'2'.repeat(64));
''')

    def test_get_response_older_than_announced_marker_is_ignored(self):
        """seamless-client.js; Record: HTTP reads cannot rewind websocket metadata."""
        run_js(r'''
let delayed=null;
fetchImpl=async()=>delayed===null?response('last'):await new Promise(resolve=>delayed=resolve);
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>ctx.a?.value==='last');
delayed=true;let changes=0;ctx.a.onchange=()=>changes++;
message(sockets[0],['update',['a','5'.repeat(64),5]]);await wait(()=>typeof delayed==='function');
delayed(response('stale',200,{'etag':'"'+('3'.repeat(64))+'"','x-seamless-marker':'3'}));
await new Promise(resolve=>realSetTimeout(resolve,25));
assert.equal(ctx.a.value,'last');assert.equal(ctx.a.marker,5);
assert.equal(ctx.a.checksum,'5'.repeat(64));assert.equal(changes,0);
''')

    @pytest.mark.parametrize("status", [200, 409])
    def test_late_put_reply_never_rewinds_newer_websocket_state(self, status):
        """seamless-client.js: late PUT replies cannot overwrite newer marker/value."""
        run_js(r'''
const pending=[];let current='1';
fetchImpl=async(url,options)=>options.method==='PUT'?await new Promise(resolve=>pending.push(resolve)):response(current,200,{'etag':'"'+((current==='five'?'5':'1').repeat(64))+'"','x-seamless-marker':current==='five'?'5':'1'});
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>ctx.a?.value==='1');
ctx.a.set('2');await wait(()=>pending.length===1);
current='five';message(sockets[0],['update',['a','5'.repeat(64),5]]);await wait(()=>ctx.a.value==='five');
pending.shift()(response(JSON.stringify({checksum:'2'.repeat(64),marker:2}),STATUS));
await new Promise(resolve=>realSetTimeout(resolve,25));
assert.equal(ctx.a.marker,5);assert.equal(ctx.a.checksum,'5'.repeat(64));assert.equal(ctx.a.value,'five');
'''.replace('STATUS',str(status)))

    def test_set_input_callback_observes_new_local_value_and_latest_pending(self):
        """seamless-client.js: oninput sees the local value and queued latest stays visible."""
        run_js(r'''
const pending=[];
fetchImpl=async(url,options)=>options.method==='PUT'?await new Promise(resolve=>pending.push(resolve)):response('1');
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>ctx.a?.value==='1');
const seen=[];ctx.a.oninput=()=>seen.push(ctx.a.value);
ctx.a.set('2');await wait(()=>pending.length===1);
ctx.a.set('3');ctx.a.set('4');
assert.deepEqual(seen,['2','3','4']);assert.equal(ctx.a.value,'4');
pending.shift()(response(JSON.stringify({checksum:'2'.repeat(64),marker:2})));
await wait(()=>puts().length===2);
assert.equal(puts()[1].options.body,'4');assert.equal(ctx.a.value,'4');
pending.shift()(response(JSON.stringify({checksum:'4'.repeat(64),marker:3})));
await wait(()=>ctx.a.marker===3);assert.equal(ctx.a.value,'4');
''')

    def test_pending_put_uses_newest_marker_after_late_ack(self):
        """seamless-client.js: latest pending write uses the newest websocket marker."""
        run_js(r'''
const pending=[];let current='1';
fetchImpl=async(url,options)=>options.method==='PUT'?await new Promise(resolve=>pending.push(resolve)):response(current,200,{'x-seamless-marker':current==='five'?'5':'1','etag':'"'+((current==='five'?'5':'1').repeat(64))+'"'});
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>ctx.a?.value==='1');
ctx.a.set('2');await wait(()=>pending.length===1);
current='five';message(sockets[0],['update',['a','5'.repeat(64),5]]);await wait(()=>ctx.a.value==='five');
ctx.a.set('6');assert.equal(ctx.a.value,'6');
pending.shift()(response(JSON.stringify({checksum:'2'.repeat(64),marker:2})));
await wait(()=>puts().length===2);
assert.equal(new URL(puts()[1].url).searchParams.get('marker'),'5');
assert.equal(puts()[1].options.body,'6');assert.equal(ctx.a.value,'6');
pending.shift()(response(JSON.stringify({checksum:'6'.repeat(64),marker:6})));
await wait(()=>ctx.a.marker===6);
''')

    def test_snapshots_preserve_user_auto_read_overrides(self):
        """seamless-client.js: entries retain explicit auto_read opt-out and opt-in."""
        run_js(r'''
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a'),'page.html':share('page.html')});await wait(()=>ctx.a?.value==='1\n');
ctx.a.auto_read=false;ctx['page.html'].auto_read=true;
requests.length=0;
message(sockets[0],['shares',{a:share('a','2'.repeat(64),2),'page.html':share('page.html','2'.repeat(64),2)}]);
await wait(()=>requests.some(r=>r.url.endsWith('page.html')));
assert.equal(ctx.a.auto_read,false);assert.equal(ctx['page.html'].auto_read,true);
assert.ok(!requests.some(r=>r.url.endsWith('/a')));
''')

    def test_removed_readded_entry_ignores_old_get_completion(self):
        """seamless-client.js: detached entry reads cannot mutate its replacement."""
        run_js(r'''
const reads=[];fetchImpl=async()=>await new Promise(resolve=>reads.push(resolve));
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>reads.length===1);
const old=ctx.a;let oldChanges=0;old.onchange=()=>oldChanges++;
message(sockets[0],['shares',{}]);message(sockets[0],['shares',{a:share('a','2'.repeat(64),2)}]);
await wait(()=>reads.length===2);assert.notEqual(ctx.a,old);
reads[1](response('new',200,{'etag':'"'+('2'.repeat(64))+'"','x-seamless-marker':'2'}));await wait(()=>ctx.a.value==='new');
reads[0](response('old'));await new Promise(resolve=>realSetTimeout(resolve,25));
assert.equal(ctx.a.value,'new');assert.equal(ctx.a.marker,2);assert.equal(oldChanges,0);
''')

    def test_removed_readded_entry_ignores_old_put_and_pending_work(self):
        """seamless-client.js: detached entry PUT completion cannot send queued work."""
        run_js(r'''
const pending=[];let currentMarker=1;fetchImpl=async(url,options)=>options.method==='PUT'?await new Promise(resolve=>pending.push(resolve)):response('current',200,{'x-seamless-marker':String(currentMarker),'etag':'"'+(String(currentMarker).repeat(64))+'"'});
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>ctx.a?.value==='current');
const old=ctx.a;old.set('old');await wait(()=>pending.length===1);old.set('queued');
message(sockets[0],['shares',{}]);currentMarker=9;message(sockets[0],['shares',{a:share('a','9'.repeat(64),9)}]);
await wait(()=>ctx.a!==old && ctx.a.value==='current');
pending.shift()(response(JSON.stringify({checksum:'2'.repeat(64),marker:2})));
await new Promise(resolve=>realSetTimeout(resolve,25));
assert.equal(puts().length,1);assert.equal(ctx.a.marker,9);assert.equal(ctx.a.checksum,'9'.repeat(64));
''')

    def test_manual_connect_invalidates_old_socket_messages(self):
        """seamless-client.js: reconnect adopts one socket generation at a time."""
        run_js(r'''
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{a:share('a')});await wait(()=>ctx.a?.value==='1\n');
const old=sockets[0];ctx.self.connect();await wait(()=>sockets.length===2);
assert.equal(old.readyState,FakeWebSocket.CLOSED);
snapshot(sockets[1],{a:share('a','2'.repeat(64),2)});await wait(()=>ctx.a.marker===2);
message(old,['shares',{a:share('a','9'.repeat(64),9),ghost:share('ghost')}]);
message(old,['update',['a','9'.repeat(64),9]]);
await new Promise(resolve=>realSetTimeout(resolve,25));
assert.equal(ctx.a.marker,2);assert.equal(ctx.a.checksum,'2'.repeat(64));assert.ok(!ctx.ghost);
''')


class TestShareClientJSConflictRead:
    pytestmark = pytest.mark.skipif(NODE is None, reason="Node is unavailable")

    @pytest.mark.parametrize("key", ["asset.txt", "a"])
    def test_conflict_forces_refresh_without_changing_auto_read_preference(self, key):
        """seamless-client.js: 409 fetches server value even for opt-out entries."""
        run_js(r'''
const key=KEY;
let current='server', currentMarker=1;
fetchImpl=async(url,options)=>options.method==='PUT'?(current='foreign',currentMarker=9,response(JSON.stringify({checksum:'9'.repeat(64),marker:9}),409)):response(current,200,{'x-seamless-marker':String(currentMarker),'etag':'"'+String(currentMarker).repeat(64)+'"'});
loadClient();const ctx=connect_seamless();await wait(()=>sockets.length===1);
snapshot(sockets[0],{[key]:share(key)});await wait(()=>ctx[key]);
ctx[key].auto_read=false;
requests.length=0;
ctx[key].set('local');await wait(()=>ctx[key].value==='foreign');
assert.equal(ctx[key].marker,9);assert.equal(ctx[key].checksum,'9'.repeat(64));
assert.equal(ctx[key].auto_read,false);
assert.equal(puts().length,1);
assert.equal(requests.filter(r=>(r.options.method||'GET')==='GET').length,1);
message(sockets[0],['update',[key,'8'.repeat(64),10]]);
await new Promise(resolve=>realSetTimeout(resolve,25));
assert.equal(requests.filter(r=>(r.options.method||'GET')==='GET').length,1);
'''.replace('KEY',json.dumps(key)))
