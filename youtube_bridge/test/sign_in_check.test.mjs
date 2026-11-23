/**
 * Reading youtube.com's own answer to "is this session signed in?".
 *
 * The cases are the ones measured on 2026-11-23: a live session, an ended one,
 * a forged one and none at all, all sent to youtube.com/account with redirects
 * off. The bot used to treat "a cookie file exists" as "signed in", so a session
 * Google had ended read as connected for days.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { signedInVerdict } from '../media.mjs';

const page = (cfg) => `<html><script>ytcfg.set(${JSON.stringify(cfg)});</script></html>`;

test('a live session is signed in and carries its DataSync ID', () => {
  const verdict = signedInVerdict({
    status: 200,
    body: page({ LOGGED_IN: true, DATASYNC_ID: '100492974012225875348||' })
  });
  assert.deepEqual(verdict, { state: 'signed_in', datasyncId: '100492974012225875348||' });
});

test('an ended session is sent to Google sign-in, which is signed out', () => {
  const verdict = signedInVerdict({
    status: 303,
    location: 'https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2Faccount'
  });
  assert.equal(verdict.state, 'signed_out');
});

test('a page that says LOGGED_IN false is signed out', () => {
  assert.equal(signedInVerdict({ status: 200, body: page({ LOGGED_IN: false }) }).state, 'signed_out');
});

test('a redirect somewhere other than Google sign-in says nothing about the session', () => {
  for (const location of [
    'https://consent.youtube.com/m?continue=https://www.youtube.com/account',
    'https://www.youtube.com/',
    'https://accounts.google.com.example.net/ServiceLogin',
    'not a url at all ::'
  ]) {
    assert.equal(signedInVerdict({ status: 302, location }).state, 'unknown', location);
  }
});

test('an outage, a rate limit or an unreadable page is unknown, never signed out', () => {
  for (const status of [429, 500, 503, 0]) {
    assert.equal(signedInVerdict({ status }).state, 'unknown', String(status));
  }
  assert.equal(signedInVerdict({ status: 200, body: '<html>no config here</html>' }).state, 'unknown');
});
