import test from 'node:test';
import assert from 'node:assert/strict';
import { enqueueRemoteIce, flushRemoteIce, MAX_REMOTE_ICE_CANDIDATES } from '../ice-queue.mjs';

function session() {
  return { cancelled: false, pc: null, remoteReady: false, iceQueue: [], flushingIce: false, flushPromise: null };
}

test('queues trickle ICE before RTCPeerConnection and applies it only after remote SDP', async () => {
  const state = session();
  assert.equal(enqueueRemoteIce(state, { candidate: 'early-1' }), true);
  assert.equal(enqueueRemoteIce(state, { candidate: 'early-2' }), true);
  const added = [];
  state.pc = { remoteDescription: null, async addIceCandidate(candidate) { added.push(candidate.candidate); } };
  state.remoteReady = true;
  await flushRemoteIce(state);
  assert.deepEqual(added, []);
  state.pc.remoteDescription = { type: 'answer', sdp: 'applied' };
  await flushRemoteIce(state);
  assert.deepEqual(added, ['early-1', 'early-2']);
  assert.deepEqual(state.iceQueue, []);
});

test('preserves candidate order when a new ICE event arrives during queue drain', async () => {
  const state = session();
  let releaseFirst;
  const firstAdd = new Promise((resolve) => { releaseFirst = resolve; });
  const added = [];
  state.pc = {
    remoteDescription: { type: 'answer', sdp: 'applied' },
    async addIceCandidate(candidate) {
      added.push(candidate.candidate);
      if (candidate.candidate === 'first') await firstAdd;
    },
  };
  state.remoteReady = true;
  enqueueRemoteIce(state, { candidate: 'first' });
  const draining = flushRemoteIce(state);
  await Promise.resolve();
  assert.equal(await enqueueRemoteIce(state, { candidate: 'second' }), true);
  releaseFirst();
  await draining;
  assert.deepEqual(added, ['first', 'second']);
});

test('ignores late ICE after membership is cancelled and caps the queue', async () => {
  const state = session();
  for (let index = 0; index < MAX_REMOTE_ICE_CANDIDATES; index += 1) {
    assert.equal(enqueueRemoteIce(state, { candidate: `c${index}` }), true);
  }
  assert.equal(enqueueRemoteIce(state, { candidate: 'overflow' }), false);
  state.cancelled = true;
  assert.equal(enqueueRemoteIce(state, { candidate: 'late' }), false);
  const added = [];
  state.pc = { remoteDescription: { type: 'answer', sdp: 'applied' }, async addIceCandidate(candidate) { added.push(candidate); } };
  state.remoteReady = true;
  await flushRemoteIce(state, () => !state.cancelled);
  assert.deepEqual(added, []);
  assert.equal(state.iceQueue.length, MAX_REMOTE_ICE_CANDIDATES);
});
