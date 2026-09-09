import assert from 'node:assert/strict';
import test from 'node:test';

import {
  musicItemPayload,
  normalizeSearchKey,
  streamCacheTtlMs
} from '../media.mjs';

test('normalizes equivalent search queries per mode', () => {
  assert.equal(normalizeSearchKey('music', '  Ela   VEM  '), 'music:ela vem');
  assert.equal(normalizeSearchKey('video', 'Ela Vem'), 'video:ela vem');
});

test('maps YouTube Music search items to the bridge contract', () => {
  const payload = musicItemPayload({
    id: 'J0pZqX0ITxg',
    title: 'Ela Vem',
    artists: [{ name: 'Mc G15' }, { name: 'Mc Livinho' }],
    duration: { seconds: 197 }
  });

  assert.deepEqual(payload, {
    id: 'J0pZqX0ITxg',
    videoId: 'J0pZqX0ITxg',
    title: 'Ela Vem',
    uploader: 'Mc G15, Mc Livinho',
    artists: [{ name: 'Mc G15' }, { name: 'Mc Livinho' }],
    duration: 197,
    webpage_url: 'https://www.youtube.com/watch?v=J0pZqX0ITxg'
  });
});

test('maps Up Next items using their video_id and author', () => {
  const payload = musicItemPayload({
    video_id: '48Lrud3Bxpc',
    title: { toString: () => 'Ela Vem (SET DJ NENE)' },
    author: 'Mc Kevin',
    duration: { seconds: 288 }
  });

  assert.equal(payload.videoId, '48Lrud3Bxpc');
  assert.equal(payload.title, 'Ela Vem (SET DJ NENE)');
  assert.equal(payload.uploader, 'Mc Kevin');
});

test('derives stream cache TTL from URL expiry with safety margin and cap', () => {
  const nowMs = 1_800_000_000_000;
  const expiresAt = Math.floor(nowMs / 1000) + 1_800;
  const url = `https://example.test/audio?expire=${expiresAt}`;

  assert.equal(streamCacheTtlMs(url, nowMs), 1_680_000);
});

test('caps long stream validity and rejects already unsafe URLs', () => {
  const nowMs = 1_800_000_000_000;
  const farExpiry = Math.floor(nowMs / 1000) + 10_000;
  const nearExpiry = Math.floor(nowMs / 1000) + 60;

  assert.equal(streamCacheTtlMs(`https://x.test/?expire=${farExpiry}`, nowMs), 3_600_000);
  assert.equal(streamCacheTtlMs(`https://x.test/?expire=${nearExpiry}`, nowMs), 0);
  assert.equal(streamCacheTtlMs('https://x.test/no-expiry', nowMs), 300_000);
});
