import assert from 'node:assert/strict';
import test from 'node:test';

import { ExpiringLruCache } from '../cache.mjs';

test('expires entries at their individual deadline', () => {
  let now = 1_000;
  const cache = new ExpiringLruCache({ maxEntries: 2, now: () => now });

  cache.set('song', { id: 1 }, 50);
  assert.deepEqual(cache.get('song'), { id: 1 });

  now = 1_050;
  assert.equal(cache.get('song'), undefined);
});

test('evicts the least recently used entry', () => {
  const cache = new ExpiringLruCache({ maxEntries: 2 });

  cache.set('first', 1, 1_000);
  cache.set('second', 2, 1_000);
  assert.equal(cache.get('first'), 1);
  cache.set('third', 3, 1_000);

  assert.equal(cache.get('second'), undefined);
  assert.equal(cache.get('first'), 1);
  assert.equal(cache.get('third'), 3);
});

test('deduplicates pending work and caches its result', async () => {
  const cache = new ExpiringLruCache({ maxEntries: 2 });
  let calls = 0;
  const create = async () => {
    calls += 1;
    await Promise.resolve();
    return { id: calls };
  };

  const [first, second] = await Promise.all([
    cache.getOrCreate('song', 1_000, create),
    cache.getOrCreate('song', 1_000, create)
  ]);
  const third = await cache.getOrCreate('song', 1_000, create);

  assert.equal(calls, 1);
  assert.strictEqual(first, second);
  assert.strictEqual(second, third);
});

test('does not cache rejected work', async () => {
  const cache = new ExpiringLruCache({ maxEntries: 2 });
  let calls = 0;

  await assert.rejects(cache.getOrCreate('song', 1_000, async () => {
    calls += 1;
    throw new Error('temporary failure');
  }));

  assert.equal(await cache.getOrCreate('song', 1_000, async () => {
    calls += 1;
    return 'recovered';
  }), 'recovered');
  assert.equal(calls, 2);
});
