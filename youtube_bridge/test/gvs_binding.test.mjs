import assert from 'node:assert/strict';
import test from 'node:test';

import {
  CAP_PROBE_OFFSET,
  capProbeOffset,
  gvsBindingCandidates,
  probeVerdict
} from '../media.mjs';

const base = 'https://rr1---sn-x.googlevideo.com/videoplayback?itag=251&c=MWEB';

test('video-bound comes first by default, as [055] measured', () => {
  const names = gvsBindingCandidates({ videoId: 'abc', sessionBinding: 'ds', visitorData: 'vd' })
    .map((c) => c.name);
  assert.deepEqual(names, ['video', 'session', 'visitor']);
});

test('the session candidate keeps the URL\'s own pot rather than minting one', () => {
  const session = gvsBindingCandidates({ videoId: 'abc' }).find((c) => c.name === 'session');
  assert.equal(session.binding, null);
});

test('a binding that worked before is tried first', () => {
  const names = gvsBindingCandidates({ videoId: 'abc', visitorData: 'vd', preferred: 'session' })
    .map((c) => c.name);
  assert.deepEqual(names, ['session', 'video', 'visitor']);
});

test('visitorData is not tried twice when it is the session binding', () => {
  const names = gvsBindingCandidates({ videoId: 'abc', sessionBinding: 'vd', visitorData: 'vd' })
    .map((c) => c.name);
  assert.deepEqual(names, ['video', 'session']);
});

test('a long file is probed past the cap', () => {
  assert.equal(capProbeOffset(`${base}&clen=3920728`), CAP_PROBE_OFFSET);
  assert.ok(CAP_PROBE_OFFSET > 1_181_250, 'past the highest cut-off seen on danielBot');
});

test('a short file, a live broadcast and an unknown length are not probed', () => {
  assert.equal(capProbeOffset(`${base}&clen=900000`), null);
  assert.equal(capProbeOffset(`${base}&live=1&clen=9999999`), null);
  assert.equal(capProbeOffset(`${base}&noclen=1`), null);
  assert.equal(capProbeOffset(base), null);
  assert.equal(capProbeOffset('not a url'), null);
});

test('206 is served, 403 is the cap, anything else is inconclusive', () => {
  assert.equal(probeVerdict(206), true);
  assert.equal(probeVerdict(403), false);
  assert.equal(probeVerdict(503), null);
});
