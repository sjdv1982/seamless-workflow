"""Focused browser-client restart and reconnect contract cases."""

from test_share_client_js import run_js


RECONNECT = r'''
const reconnect = async () => {
  const previous = sockets.length;
  sockets[previous-1].close();
  await wait(() => timers.length > 0);
  const timer = timers.shift();
  timer.fn(...timer.args);
  await wait(() => sockets.length === previous + 1);
  return sockets[previous];
};
const timers = [];
global.setTimeout = (fn, delay, ...args) => {
  timers.push({fn, delay, args});
  return timers.length;
};
global.clearTimeout = () => {};
'''


def test_restarted_server_adopts_markers_without_rereading_retained_value():
    run_js(RECONNECT + r'''
let reads = 0;
fetchImpl = async () => { reads++; return response('original', 200, {
  'etag':'"'+('7'.repeat(64))+'"', 'x-seamless-marker':'70'}); };
loadClient(); const ctx = connect_seamless(); await wait(() => sockets.length === 1);
snapshot(sockets[0], {a:share('a','7'.repeat(64),70)});
await wait(() => ctx.a?.value === 'original');
const originalChecksum = ctx.a.checksum;
let input = 0, changes = 0;
ctx.a.oninput = () => input++;
ctx.a.onchange = () => changes++;
const readsBefore = reads;
const next = await reconnect();
snapshot(next, {a:share('a','1'.repeat(64),1)});
await wait(() => ctx.a.marker === 1);
assert.equal(ctx.a.value, 'original');
assert.equal(ctx.a.checksum, originalChecksum);
assert.equal(reads, readsBefore);
assert.equal(input, 0);
assert.equal(changes, 0);
''')


def test_same_server_reconnect_rereads_auto_read_value():
    run_js(RECONNECT + r'''
let value = 'before', reads = 0;
fetchImpl = async () => { reads++; return response(value, 200, {
  'etag':'"'+('5'.repeat(64))+'"', 'x-seamless-marker':'5'}); };
loadClient(); const ctx = connect_seamless(); await wait(() => sockets.length === 1);
snapshot(sockets[0], {a:share('a','5'.repeat(64),5)});
await wait(() => ctx.a?.value === 'before');
const readsBefore = reads;
value = 'after';
const next = await reconnect();
snapshot(next, {a:share('a','5'.repeat(64),5)});
await wait(() => ctx.a.value === 'after');
assert.equal(reads, readsBefore + 1);
assert.equal(ctx.a.marker, 5);
''')


def test_one_own_put_marker_lag_is_tolerated():
    run_js(RECONNECT + r'''
let reads = 0;
fetchImpl = async (url, options) => {
  if (options.method === 'PUT') return response(JSON.stringify({
    checksum:'6'.repeat(64), marker:6}));
  reads++;
  return response('initial', 200, {
    'etag':'"'+('5'.repeat(64))+'"', 'x-seamless-marker':'5'});
};
loadClient(); const ctx = connect_seamless(); await wait(() => sockets.length === 1);
snapshot(sockets[0], {a:share('a','5'.repeat(64),5)});
await wait(() => ctx.a?.value === 'initial');
ctx.a.set('local');
await wait(() => ctx.a.marker === 6);
const readsBefore = reads;
const next = await reconnect();
snapshot(next, {a:share('a','5'.repeat(64),5)});
await wait(() => reads > readsBefore);
assert.equal(ctx.a.marker, 6);
message(next, ['update',['a','6'.repeat(64),6]]);
await wait(() => ctx.a.checksum === '6'.repeat(64));
''')


def test_lower_marker_without_own_put_is_a_restart():
    run_js(RECONNECT + r'''
let reads = 0;
fetchImpl = async () => { reads++; return response('kept', 200, {
  'etag':'"'+('6'.repeat(64))+'"', 'x-seamless-marker':'6'}); };
loadClient(); const ctx = connect_seamless(); await wait(() => sockets.length === 1);
snapshot(sockets[0], {a:share('a','6'.repeat(64),6)});
await wait(() => ctx.a?.value === 'kept');
const readsBefore = reads;
const next = await reconnect();
snapshot(next, {a:share('a','5'.repeat(64),5)});
await wait(() => ctx.a.marker === 5);
assert.equal(ctx.a.value, 'kept');
assert.equal(ctx.a.checksum, '6'.repeat(64));
assert.equal(reads, readsBefore);
''')


def test_restart_reads_new_never_read_and_uncorrected_conflict_keys():
    run_js(RECONNECT + r'''
let phase = 'initial';
let conflicted = false;
const reads = [];
fetchImpl = async (url, options) => {
  const key = url.split('/').pop().split('?')[0];
  if (options.method === 'PUT') {
    conflicted = true;
    return response(JSON.stringify({checksum:'1'.repeat(64), marker:1}), 409);
  }
  reads.push([phase, key]);
  if (phase === 'initial' && key === 'unread') return response('missing', 503);
  const body = phase === 'initial' ? 'original' : key + '-recovered';
  const marker = phase === 'initial' && !(key === 'conflict' && conflicted) ? 70 : 1;
  return response(body, 200, {
    'etag':'"'+((marker === 70 ? '7' : '1').repeat(64))+'"',
    'x-seamless-marker': String(marker)});
};
loadClient(); const ctx = connect_seamless(); await wait(() => sockets.length === 1);
snapshot(sockets[0], {
  retained:share('retained','7'.repeat(64),70),
  unread:share('unread','7'.repeat(64),70),
  conflict:share('conflict','7'.repeat(64),70)
});
await wait(() => ctx.retained?.value === 'original' &&
                 ctx.conflict?.value === 'original' &&
                 reads.some(([p,k]) => p === 'initial' && k === 'unread'));
ctx.conflict.set('local-edit');
await wait(() => puts().length === 1);
await wait(() => reads.filter(([p,k]) => p === 'initial' && k === 'conflict').length >= 2);
phase = 'restarted';
const next = await reconnect();
snapshot(next, {
  retained:share('retained','1'.repeat(64),1),
  unread:share('unread','1'.repeat(64),1),
  conflict:share('conflict','1'.repeat(64),1),
  added:share('added','1'.repeat(64),1)
});
await wait(() => ctx.unread?.value === 'unread-recovered' &&
                 ctx.conflict?.value === 'conflict-recovered' &&
                 ctx.added?.value === 'added-recovered');
assert.equal(ctx.retained.value, 'original');
assert.ok(!reads.some(([p,k]) => p === 'restarted' && k === 'retained'));
for (const key of ['unread','conflict','added'])
  assert.ok(reads.some(([p,k]) => p === 'restarted' && k === key), key);
''')


def test_delayed_live_shares_snapshot_cannot_regress_update_marker():
    run_js(r'''
fetchImpl = async () => response('initial', 200, {
  'etag':'"'+('5'.repeat(64))+'"', 'x-seamless-marker':'5'});
loadClient(); const ctx = connect_seamless(); await wait(() => sockets.length === 1);
snapshot(sockets[0], {a:share('a','5'.repeat(64),5)});
await wait(() => ctx.a?.value === 'initial');
message(sockets[0], ['update',['a','9'.repeat(64),9]]);
await wait(() => ctx.a.marker === 9);
message(sockets[0], ['shares',{a:share('a','6'.repeat(64),6)}]);
await new Promise(resolve => realSetTimeout(resolve,25));
assert.equal(ctx.a.marker, 9);
assert.equal(ctx.a.checksum, '9'.repeat(64));
''')


def test_restart_invalidates_inflight_get_from_old_server():
    run_js(RECONNECT + r'''
let pendingOld;
let reads = 0;
fetchImpl = async () => {
  reads++;
  if (reads === 2) return await new Promise(resolve => pendingOld = resolve);
  return response('retained', 200, {
    'etag':'"'+('7'.repeat(64))+'"', 'x-seamless-marker':'70'});
};
loadClient(); const ctx = connect_seamless(); await wait(() => sockets.length === 1);
snapshot(sockets[0], {a:share('a','7'.repeat(64),70)});
await wait(() => ctx.a?.value === 'retained');
let input = 0, changes = 0;
ctx.a.oninput = () => input++;
ctx.a.onchange = () => changes++;
message(sockets[0], ['update',['a','8'.repeat(64),71]]);
await wait(() => pendingOld);
const next = await reconnect();
snapshot(next, {a:share('a','1'.repeat(64),1)});
await wait(() => ctx.a.marker === 1);
pendingOld(response('obsolete', 200, {
  'etag':'"'+('8'.repeat(64))+'"', 'x-seamless-marker':'71'}));
await new Promise(resolve => realSetTimeout(resolve,25));
assert.equal(ctx.a.value, 'retained');
assert.equal(ctx.a.checksum, '8'.repeat(64));
assert.equal(input, 0);
assert.equal(changes, 0);
''')


def test_optimistic_value_never_read_from_server_is_read_after_restart():
    run_js(RECONNECT + r'''
let restarted = false;
let recoveryReads = 0;
fetchImpl = async (url, options) => {
  if (options.method === 'PUT') return response(JSON.stringify({
    checksum:'8'.repeat(64), marker:71}));
  if (!restarted) return response('unavailable',503);
  recoveryReads++;
  return response('server-restored',200,{
    'etag':'"'+('1'.repeat(64))+'"', 'x-seamless-marker':'1'});
};
loadClient(); const ctx = connect_seamless(); await wait(() => sockets.length === 1);
snapshot(sockets[0], {a:share('a','7'.repeat(64),70)});
await wait(() => ctx.a && requests.some(r => r.url.endsWith('/a')));
ctx.a.set('optimistic');
await wait(() => ctx.a.marker === 71);
assert.equal(ctx.a.value, 'optimistic');
restarted = true;
const next = await reconnect();
snapshot(next, {a:share('a','1'.repeat(64),1)});
await wait(() => ctx.a.value === 'server-restored');
assert.equal(ctx.a.marker, 1);
assert.ok(recoveryReads >= 1);
''')
