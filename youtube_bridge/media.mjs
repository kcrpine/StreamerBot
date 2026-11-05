const STREAM_CACHE_FALLBACK_TTL_MS = 5 * 60 * 1000;
const STREAM_CACHE_MAX_TTL_MS = 60 * 60 * 1000;
const STREAM_EXPIRY_SAFETY_MS = 2 * 60 * 1000;

function text(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  if (typeof value.text === 'string') return value.text;
  if (typeof value.toString === 'function') return value.toString();
  return '';
}

export function normalizeSearchKey(mode, query) {
  const normalized = String(query || '').trim().replace(/\s+/g, ' ').toLocaleLowerCase();
  return `${mode}:${normalized}`;
}

export function musicItemPayload(item) {
  const id = item?.id || item?.video_id || item?.videoId || '';
  if (!/^[A-Za-z0-9_-]{11}$/.test(id)) return null;
  const artists = Array.isArray(item.artists)
    ? item.artists
      .map((artist) => ({ name: text(artist?.name) }))
      .filter((artist) => artist.name)
    : [];
  const uploader = artists.map((artist) => artist.name).join(', ') || text(item.author);

  return {
    id,
    videoId: id,
    title: text(item.title),
    uploader,
    artists,
    duration: Number(item?.duration?.seconds || item?.duration_seconds || 0),
    webpage_url: `https://www.youtube.com/watch?v=${id}`
  };
}

export function streamCacheTtlMs(streamUrl, nowMs = Date.now()) {
  try {
    const expiresAtSeconds = Number(new URL(streamUrl).searchParams.get('expire'));
    if (!Number.isFinite(expiresAtSeconds) || expiresAtSeconds <= 0) {
      return STREAM_CACHE_FALLBACK_TTL_MS;
    }
    const safeTtl = expiresAtSeconds * 1000 - nowMs - STREAM_EXPIRY_SAFETY_MS;
    return Math.max(0, Math.min(safeTtl, STREAM_CACHE_MAX_TTL_MS));
  } catch {
    return STREAM_CACHE_FALLBACK_TTL_MS;
  }
}

// ---------------------------------------------------------------------------
// Which clients to ask for a stream, in order, and on which session.
//
// Two rules shape this list, both learned from a bot that could search but
// could not play anything:
//
// 1. Every entry must be a distinct, valid client. The chain used to read
//    ['YTMUSIC', 'MWEB', ClientType.TV_EMBEDDED], which looked like three
//    fallbacks and was one: the caller rewrote 'YTMUSIC' to MWEB, so the first
//    two sent an identical request and returned an identical error, and the
//    third was rejected inside youtubei.js before it left the process. A chain
//    that retries the same request is not a fallback.
//
// 2. An anonymous retry has to be in it. YouTube's player endpoint answered 400
//    to every OAuth-authenticated request, so signing in took ordinary playback
//    down with it. Phase 9 replaced OAuth with a real browser session, which is
//    what YouTube still serves from an untrusted address, but the rule stands:
//    a session Google has just ended fails the same way. The signed-in attempts
//    come first, because they are the only ones that can return age-restricted
//    or member content, and the only ones a datacenter address is served at
//    all; the anonymous ones come after, because they are what works on a host
//    YouTube trusts when the account is the problem.
//
// The client names are the short keys youtubei.js validates against
// (Constants.SUPPORTED_CLIENTS), NOT the ClientType enum values. ClientType is
// the vocabulary for Innertube.create({ client_type }); getBasicInfo's `client`
// option takes these. The two look interchangeable and are not.
//
// YTMUSIC is deliberately absent: its player rejects anything that is not a
// music track ("Video unavailable"), so music requests use MWEB here too. WEB
// is last because it is SABR-only for many videos in 2026.
export const AUTHENTICATED_PLAYBACK_CLIENTS = ['MWEB', 'TV_EMBEDDED'];
export const ANONYMOUS_PLAYBACK_CLIENTS = ['MWEB', 'IOS', 'WEB'];

// Embedded-player clients reject a po_token.
const CLIENTS_WITHOUT_PO_TOKEN = new Set(['TV_EMBEDDED', 'WEB_EMBEDDED']);

export function clientTakesPoToken(client) {
  return !CLIENTS_WITHOUT_PO_TOKEN.has(client);
}

/**
 * The ordered attempts for one resolution.
 *
 * Pure on purpose: which clients get tried, in which order, on which session is
 * the part that was wrong and the part worth pinning in tests. Returns
 * descriptors; the caller supplies the sessions.
 *
 * @param {{signedIn: boolean}} state
 * @returns {{client: string, session: 'authenticated'|'anonymous', label: string}[]}
 */
export function planPlaybackAttempts({ signedIn }) {
  const attempts = [];

  for (const client of AUTHENTICATED_PLAYBACK_CLIENTS) {
    attempts.push({
      client,
      session: 'authenticated',
      label: signedIn ? `${client}/signed-in` : client
    });
  }

  if (signedIn) {
    // A genuinely different request, so worth making.
    for (const client of ANONYMOUS_PLAYBACK_CLIENTS) {
      attempts.push({ client, session: 'anonymous', label: `${client}/anonymous` });
    }
  } else {
    // The session is already anonymous, so only the clients not tried above add
    // anything. Repeating MWEB here would be the old bug in a new place.
    for (const client of ANONYMOUS_PLAYBACK_CLIENTS) {
      if (AUTHENTICATED_PLAYBACK_CLIENTS.includes(client)) continue;
      attempts.push({ client, session: 'authenticated', label: client });
    }
  }

  return attempts;
}

// ---------------------------------------------------------------------------
// A bot's browser session, as youtubei.js takes it.
// ---------------------------------------------------------------------------

const YOUTUBE_DOMAIN = /(^|\.)youtube\.com$/i;

/**
 * A Netscape cookies.txt file as the Cookie header youtubei.js's `cookie`
 * option expects: "name=value; name=value".
 *
 * Only youtube.com cookies, because those are the ones a browser sends to
 * youtube.com; the file also carries google.com cookies for the bot's own
 * Chrome. Expired cookies are dropped. Where the same name appears on several
 * youtube.com domains, the most specific domain wins, as it does in a browser.
 *
 * Pure, and the only place this format is read on the Node side.
 *
 * @param {string} text
 * @param {number} [nowSeconds]
 * @returns {string}
 */
export function cookieHeaderFromNetscape(text, nowSeconds = Date.now() / 1000) {
  const chosen = new Map();
  for (const raw of String(text || '').replace(/^\uFEFF/, '').split(/\r?\n/)) {
    let line = raw;
    if (line.startsWith('#HttpOnly_')) line = line.slice('#HttpOnly_'.length);
    if (!line.trim() || line.startsWith('#')) continue;
    const fields = line.split('\t');
    if (fields.length !== 7) continue;
    const [domain, , , , expires, name, value] = fields;
    const bare = domain.replace(/^\./, '');
    if (!name || !YOUTUBE_DOMAIN.test(bare)) continue;
    const expiry = Number(expires);
    if (Number.isFinite(expiry) && expiry > 0 && expiry < nowSeconds) continue;
    const previous = chosen.get(name);
    if (!previous || bare.length > previous.domainLength) {
      chosen.set(name, { value, domainLength: bare.length });
    }
  }
  return [...chosen.entries()].map(([name, { value }]) => `${name}=${value}`).join('; ');
}

/**
 * What a proof-of-origin token must be bound to for this session.
 *
 * A signed-out session is identified by its visitorData ([015]). A signed-in
 * one is identified by the account's DataSync ID, and a token bound to anything
 * else is silently ignored, which from a datacenter address means
 * LOGIN_REQUIRED on every client. The DataSync ID comes from ytcfg in the bot's
 * own Chrome, stored beside the cookies.
 *
 * @param {{signedIn: boolean, visitorData?: string, datasyncId?: string}} session
 * @returns {string|undefined}
 */
export function contentBindingFor({ signedIn, visitorData, datasyncId }) {
  if (signedIn) return datasyncId || undefined;
  return visitorData || undefined;
}

/**
 * Put a proof-of-origin token on a resolved `videoplayback` URL.
 *
 * This is the *second* token, and it is bound to the video rather than to the
 * session. Measured against real resolved URLs on 2026-09-21: a URL carrying
 * the session-bound token and a URL carrying no token at all behave
 * identically — the first ~1,024,000 bytes are served and every byte past that
 * is a flat 403, whatever offset or window asks for it. Replace `pot` with a
 * token bound to the video ID and the same URL serves the whole remainder of
 * the file in one open-ended request. A token whose binding does not match is
 * not an error; it is ignored, which is why the broken case looked exactly like
 * the un-attested one.
 *
 * YouTube already puts a `pot` on the URL, so this replaces rather than
 * appends; appending a second one leaves the first to win.
 *
 * @param {string} streamUrl
 * @param {string} token
 * @returns {string} the URL with `pot` set, or unchanged if there is no token
 */
export function withGvsPoToken(streamUrl, token) {
  if (!streamUrl || !token) return streamUrl;
  const encoded = encodeURIComponent(token);
  if (/[?&]pot=/.test(streamUrl)) {
    return streamUrl.replace(/([?&]pot=)[^&]*/, `$1${encoded}`);
  }
  return `${streamUrl}${streamUrl.includes('?') ? '&' : '?'}pot=${encoded}`;
}

/**
 * The bindings to try for the stream URL's token, best guess first.
 *
 * [055] measured on 2026-09-21 that a video-bound token lifts the ~1 MB cap and
 * a session-bound one does not. On 2026-11-04 that stopped being true for a
 * signed-in bot: danielBot's every video carried a fresh video-bound token and
 * was still cut off at 962,500–1,181,250 bytes, about a minute in. yt-dlp
 * documents the signed-in rule the other way round (session-bound). Rather than
 * swap one hard-coded guess for another, the bridge tries these in order and
 * keeps the first that Google actually serves past the cap (see
 * servesPastCap). `binding: null` means "keep the session-bound `pot` YouTube
 * already put on the URL", which is the session-bound case.
 *
 * @param {{videoId: string, sessionBinding?: string, visitorData?: string, preferred?: string}} input
 * @returns {{name: 'video'|'session'|'visitor', binding: string|null}[]}
 */
export function gvsBindingCandidates({ videoId, sessionBinding, visitorData, preferred }) {
  const all = [];
  if (videoId) all.push({ name: 'video', binding: videoId });
  all.push({ name: 'session', binding: null });
  if (visitorData && visitorData !== sessionBinding) all.push({ name: 'visitor', binding: visitorData });
  const first = all.findIndex((c) => c.name === preferred);
  if (first > 0) all.unshift(...all.splice(first, 1));
  return all;
}

// Where to ask for a byte when checking the cap: past the highest offset the
// cap has been seen at (1,181,250 on danielBot) with room to spare.
export const CAP_PROBE_OFFSET = 1_300_000;

/**
 * Whether a probe is worth making for this URL, and where.
 *
 * A live broadcast is addressed by segment, not bytes ([056]), and a file
 * shorter than the probe offset cannot show the cap, so neither is probed.
 *
 * @param {string} streamUrl
 * @returns {number|null} the byte to ask for, or null for "do not probe"
 */
export function capProbeOffset(streamUrl) {
  let url;
  try { url = new URL(streamUrl); } catch { return null; }
  const q = url.searchParams;
  if (q.get('live') === '1' || q.get('noclen') === '1') return null;
  const clen = Number(q.get('clen'));
  if (!Number.isFinite(clen) || clen <= CAP_PROBE_OFFSET + 1000) return null;
  return CAP_PROBE_OFFSET;
}

/**
 * Read a probe's HTTP status: true served, false capped, null inconclusive.
 * @param {number} status
 */
export function probeVerdict(status) {
  if (status === 206 || status === 200) return true;
  if (status === 403) return false;
  return null;
}

// Path prefixes under which YouTube puts the video ID as the next segment.
// /live/ is what "Share" gives for a stream and what every failing request in
// kuhao's log used; /embed/ and /v/ are the older embed and short-link shapes.
const VIDEO_ID_PATH_PREFIXES = new Set(['shorts', 'live', 'embed', 'v']);
const VIDEO_ID_PATTERN = /^[A-Za-z0-9_-]{11}$/;

/**
 * The 11-character video ID in a YouTube URL or a bare ID, or null.
 *
 * Only accepts an ID-shaped result, so a path such as /live/ with no ID after
 * it, or /shorts/<anything long>, is "not a video" rather than a bogus lookup.
 *
 * @param {string} input
 * @returns {string|null}
 */
export function extractVideoId(input) {
  if (!input) return null;
  if (VIDEO_ID_PATTERN.test(input)) return input;
  try {
    const url = new URL(input);
    const segments = url.pathname.split('/').filter(Boolean);
    let candidate = null;
    if (url.hostname === 'youtu.be') {
      candidate = segments[0] || null;
    } else if (url.searchParams.get('v')) {
      candidate = url.searchParams.get('v');
    } else if (VIDEO_ID_PATH_PREFIXES.has(segments[0])) {
      candidate = segments[1] || null;
    }
    return candidate && VIDEO_ID_PATTERN.test(candidate) ? candidate : null;
  } catch {}
  return null;
}
