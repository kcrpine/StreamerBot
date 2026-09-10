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
// 2. An anonymous retry has to be in it. YouTube's player endpoint answers 400
//    to an OAuth-authenticated request that it serves anonymously, so signing
//    in to reach age-restricted content took ordinary playback down with it.
//    The signed-in attempts come first, because they are the only ones that can
//    return age-restricted or member content; the anonymous ones come after,
//    because they are what works when the account is the problem.
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
