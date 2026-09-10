/**
 * The client fallback chain for resolving a stream.
 *
 * A bot could search YouTube and play Icecast, but every YouTube video and
 * livestream failed with "Cannot process stream URL". Three bugs stacked up,
 * and the chain hid all three because it looked like it had fallbacks:
 *
 *   ['YTMUSIC', 'MWEB', ClientType.TV_EMBEDDED]
 *
 * The caller rewrote 'YTMUSIC' to MWEB, so entries one and two sent an
 * identical request; entry three was an enum *value*
 * ('TVHTML5_SIMPLY_EMBEDDED_PLAYER') where youtubei.js validates enum *keys*,
 * so it was rejected inside the process and had never once worked. That left one
 * real client, and when YouTube started answering 400 to that client's
 * OAuth-authenticated requests there was nothing behind it.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ANONYMOUS_PLAYBACK_CLIENTS,
  AUTHENTICATED_PLAYBACK_CLIENTS,
  clientTakesPoToken,
  planPlaybackAttempts
} from '../media.mjs';

// The names youtubei.js 18 validates against, from
// dist/src/utils/Constants.js: SUPPORTED_CLIENTS. Copied rather than imported
// because the point is to catch the day a name here stops being in that list.
const SUPPORTED_CLIENTS = [
  'IOS', 'WEB', 'MWEB', 'YTKIDS', 'YTMUSIC', 'ANDROID', 'ANDROID_VR',
  'VISIONOS', 'YTSTUDIO_ANDROID', 'YTMUSIC_ANDROID', 'TV', 'TV_SIMPLY',
  'TV_EMBEDDED', 'WEB_EMBEDDED', 'WEB_CREATOR'
];

test('every client is one youtubei.js will accept', () => {
  // The original bug: ClientType.TV_EMBEDDED is
  // 'TVHTML5_SIMPLY_EMBEDDED_PLAYER', which is not in SUPPORTED_CLIENTS, so
  // getBasicInfo threw "Invalid client" before reaching YouTube.
  for (const client of [...AUTHENTICATED_PLAYBACK_CLIENTS, ...ANONYMOUS_PLAYBACK_CLIENTS]) {
    assert.ok(
      SUPPORTED_CLIENTS.includes(client),
      `${client} is not a client youtubei.js accepts; it is probably a ` +
      'ClientType enum value rather than one of its keys'
    );
  }
});

test('no attempt repeats another attempt', () => {
  // A chain that sends the same request twice is not a fallback. This is what
  // the YTMUSIC-rewritten-to-MWEB entry did.
  for (const signedIn of [true, false]) {
    const attempts = planPlaybackAttempts({ signedIn });
    const seen = attempts.map((a) => `${a.session}:${a.client}`);
    assert.equal(
      new Set(seen).size,
      seen.length,
      `duplicate attempt with signedIn=${signedIn}: ${seen.join(', ')}`
    );
  }
});

test('a signed-in bot falls back to an anonymous session', () => {
  // The failure this exists for: YouTube answers 400 to an OAuth-authenticated
  // player request that it serves anonymously, so signing in for age-restricted
  // content broke ordinary playback with no way back.
  const attempts = planPlaybackAttempts({ signedIn: true });

  assert.ok(
    attempts.some((a) => a.session === 'anonymous'),
    'a signed-in bot with a rejected account has no route to a stream'
  );
});

test('the signed-in attempts come first', () => {
  // Age-restricted and member content only resolves on the authenticated
  // session, so trying anonymous first would silently lose access to it.
  const attempts = planPlaybackAttempts({ signedIn: true });
  const firstAnonymous = attempts.findIndex((a) => a.session === 'anonymous');
  const lastAuthenticated = attempts.reduce(
    (last, a, i) => (a.session === 'authenticated' ? i : last),
    -1
  );

  assert.ok(firstAnonymous > lastAuthenticated,
    'an anonymous attempt runs before an authenticated one');
});

test('an anonymous bot is not asked to build a second anonymous session', () => {
  // Its own session is already anonymous. A separate one would be the same
  // request through a different object.
  const attempts = planPlaybackAttempts({ signedIn: false });

  assert.ok(attempts.every((a) => a.session === 'authenticated'));
});

test('an anonymous bot still gets more than one client', () => {
  const attempts = planPlaybackAttempts({ signedIn: false });

  assert.ok(attempts.length > 1);
  assert.ok(new Set(attempts.map((a) => a.client)).size === attempts.length);
});

test('the labels say which session was used', () => {
  // The log this was diagnosed from could not distinguish "MWEB failed" from
  // "MWEB failed while signed in", which was the whole answer.
  const signed = planPlaybackAttempts({ signedIn: true });

  assert.ok(signed.some((a) => a.label.includes('signed-in')));
  assert.ok(signed.some((a) => a.label.includes('anonymous')));
});

test('embedded-player clients are not sent a po_token', () => {
  assert.equal(clientTakesPoToken('TV_EMBEDDED'), false);
  assert.equal(clientTakesPoToken('WEB_EMBEDDED'), false);
  assert.equal(clientTakesPoToken('MWEB'), true);
  assert.equal(clientTakesPoToken('IOS'), true);
});

test('YTMUSIC is not used as a playback client', () => {
  // Its player rejects anything that is not a music track with "Video
  // unavailable", which is why the caller used to rewrite it to MWEB — and that
  // rewrite is what collapsed the chain.
  const all = [...AUTHENTICATED_PLAYBACK_CLIENTS, ...ANONYMOUS_PLAYBACK_CLIENTS];

  assert.ok(!all.includes('YTMUSIC'));
});
