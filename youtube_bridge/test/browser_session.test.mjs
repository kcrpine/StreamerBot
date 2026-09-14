/**
 * A bot's browser session, as the bridge reads it (Phase 9).
 *
 * The bot writes cookies.txt from its own Chrome; the bridge turns it into the
 * Cookie header youtubei.js sends, and binds the proof-of-origin token to the
 * account rather than to the visitor. Either half wrong looks the same from
 * outside: LOGIN_REQUIRED on every client, as if nobody had signed in.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { contentBindingFor, cookieHeaderFromNetscape } from '../media.mjs';

const TAB = '\t';
const line = (...fields) => fields.join(TAB);
const FUTURE = '4102444800'; // 2100

function file(...lines) {
  return ['# Netscape HTTP Cookie File', '', ...lines].join('\n');
}

test('youtube.com cookies become one Cookie header', () => {
  const header = cookieHeaderFromNetscape(file(
    line('.youtube.com', 'TRUE', '/', 'TRUE', FUTURE, 'SAPISID', 'abc'),
    line('.youtube.com', 'TRUE', '/', 'TRUE', FUTURE, '__Secure-3PSID', 'xyz')
  ));

  assert.equal(header, 'SAPISID=abc; __Secure-3PSID=xyz');
});

test('google.com cookies are not sent to YouTube', () => {
  // They are in the file for the bot's own Chrome, which keeps the session
  // alive; a browser would never send them to youtube.com.
  const header = cookieHeaderFromNetscape(file(
    line('.google.com', 'TRUE', '/', 'TRUE', FUTURE, 'SID', 'google'),
    line('.youtube.com', 'TRUE', '/', 'TRUE', FUTURE, 'SID', 'youtube'),
    line('.notyoutube.com', 'TRUE', '/', 'TRUE', FUTURE, 'X', 'no')
  ));

  assert.equal(header, 'SID=youtube');
});

test('expired cookies are dropped, session cookies are kept', () => {
  const header = cookieHeaderFromNetscape(file(
    line('.youtube.com', 'TRUE', '/', 'TRUE', '1000', 'OLD', 'gone'),
    line('.youtube.com', 'TRUE', '/', 'TRUE', '0', 'SESSION', 'kept')
  ), 2000);

  assert.equal(header, 'SESSION=kept');
});

test('the most specific domain wins for a repeated name', () => {
  const header = cookieHeaderFromNetscape(file(
    line('.youtube.com', 'TRUE', '/', 'TRUE', FUTURE, 'PREF', 'broad'),
    line('www.youtube.com', 'FALSE', '/', 'TRUE', FUTURE, 'PREF', 'specific')
  ));

  assert.equal(header, 'PREF=specific');
});

test('what real exporters write is understood', () => {
  // curl and several browser extensions prefix HttpOnly cookies, and a file
  // saved on Windows has CRLF line endings and sometimes a byte-order mark.
  const text = String.fromCharCode(0xfeff) + file(
    '#HttpOnly_' + line('.youtube.com', 'TRUE', '/', 'TRUE', FUTURE, 'SID', 'a'),
    line('.youtube.com', 'TRUE', '/', 'TRUE', FUTURE, 'HSID', 'b')
  ).replace(/\n/g, '\r\n');

  assert.equal(cookieHeaderFromNetscape(text), 'SID=a; HSID=b');
});

test('a paste that lost its tabs produces no header rather than a wrong one', () => {
  assert.equal(cookieHeaderFromNetscape('.youtube.com TRUE / TRUE 4102444800 SID a'), '');
  assert.equal(cookieHeaderFromNetscape(''), '');
});

test('a signed-in session binds its token to the DataSync ID', () => {
  // Bound to visitorData, a signed-in request's token is silently ignored.
  assert.equal(
    contentBindingFor({ signedIn: true, visitorData: 'visitor', datasyncId: 'account||' }),
    'account||'
  );
});

test('a signed-out session binds its token to visitorData', () => {
  assert.equal(
    contentBindingFor({ signedIn: false, visitorData: 'visitor', datasyncId: 'account||' }),
    'visitor'
  );
});

test('a signed-in session with no DataSync ID has no binding, not the visitor one', () => {
  // Falling back to visitorData would mint a token YouTube ignores and hide why.
  assert.equal(contentBindingFor({ signedIn: true, visitorData: 'visitor', datasyncId: '' }), undefined);
});
