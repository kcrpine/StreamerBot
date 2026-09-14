import http from 'node:http';
import fs from 'node:fs/promises';
import { statSync } from 'node:fs';
import path from 'node:path';
import { URL } from 'node:url';
import { ClientType, Innertube, UniversalCache, Platform } from 'youtubei.js';
import { ExpiringLruCache } from './cache.mjs';
import {
  clientTakesPoToken,
  contentBindingFor,
  cookieHeaderFromNetscape,
  musicItemPayload,
  normalizeSearchKey,
  planPlaybackAttempts,
  streamCacheTtlMs
} from './media.mjs';

const HOST = process.env.YOUTUBE_BRIDGE_HOST || '127.0.0.1';
const PORT = Number(process.env.YOUTUBE_BRIDGE_PORT || 4417);
const POT_URL = process.env.POT_PROVIDER_URL || 'http://127.0.0.1:4416/get_pot';
const BOTS_ROOT = path.resolve(process.env.STREAMERBOT_BOTS_ROOT || '/bots');
const USER_AGENT = process.env.YOUTUBE_BRIDGE_USER_AGENT ||
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 Version/18.5 Mobile/15E148 Safari/604.1';

// YouTube.js 18 requires an evaluator to decipher player signatures/nsig.
Platform.shim.eval = async (data) => new Function(data.output)();

// This process is shared infrastructure: every bot on the host resolves through
// it. Node makes an unhandled rejection fatal, so one bot's failure in a
// background promise would otherwise take YouTube down for everyone. The OAuth
// device flow that first did this is gone, but the rule is about the process,
// not that flow. Log and keep serving instead.
process.on('unhandledRejection', (reason) => {
  console.error('[youtube-bridge] Unhandled rejection (continuing):', reason?.message || reason);
});
process.on('uncaughtException', (error) => {
  console.error('[youtube-bridge] Uncaught exception (continuing):', error?.stack || error);
});

const SESSION_CACHE_MAX_ENTRIES = 64;
const sessionCache = new Map();
let searchSessionPromise = null;
const RESOLVE_CACHE_MAX_ENTRIES = 1024;
const resolveCache = new ExpiringLruCache({ maxEntries: RESOLVE_CACHE_MAX_ENTRIES });
const pendingResolutions = new Map();
const SEARCH_CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours
const searchCache = new ExpiringLruCache({ maxEntries: 2048 });
const RECOMMENDATION_CACHE_TTL_MS = 30 * 60 * 1000;
const recommendationCache = new ExpiringLruCache({ maxEntries: 512 });

const DISK_CACHE_FILE = path.resolve(process.env.BRIDGE_CACHE_FILE || path.join(path.dirname(new URL(import.meta.url).pathname), 'bridge_cache.json'));

async function loadDiskCache() {
  try {
    const raw = await fs.readFile(DISK_CACHE_FILE, 'utf8');
    const parsed = JSON.parse(raw);
    if (parsed.search) searchCache.load(parsed.search);
    if (parsed.resolve) resolveCache.load(parsed.resolve);
    console.log(`[youtube-bridge] Loaded persistent disk cache (search: ${searchCache.size}, resolve: ${resolveCache.size})`);
  } catch (err) {
    if (err?.code !== 'ENOENT') {
      console.warn('[youtube-bridge] Could not load disk cache:', err.message);
    }
  }
}

let saveTimer = null;
function scheduleSaveDiskCache() {
  if (saveTimer) return;
  saveTimer = setTimeout(async () => {
    saveTimer = null;
    try {
      const data = {
        search: searchCache.dump(),
        resolve: resolveCache.dump()
      };
      await fs.writeFile(DISK_CACHE_FILE, JSON.stringify(data), 'utf8');
    } catch (err) {
      console.warn('[youtube-bridge] Could not save disk cache:', err.message);
    }
  }, 2000);
}

function json(res, status, body) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': data.length
  });
  res.end(data);
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

// ---------------------------------------------------------------------------
// Per-bot browser sessions.
//
// Phase 9 replaced the OAuth device code. YouTube stopped serving playback to
// that grant (every authenticated player request answered 400), and refuses
// anonymous playback from datacenter addresses, so a bot on a VPS could sign in
// and still play nothing. What YouTube does serve is a real browser session:
// its cookies, plus a proof-of-origin token bound to the account's DataSync ID.
//
// The bot writes both under bots/<bot_id>/youtube_auth/: cookies.txt from its
// own signed-in Chrome (or a file the user imported) and session.json with the
// DataSync ID. This process only reads them. The session cache key includes the
// cookie file's mtime, so a refreshed file rebuilds that one bot's session on
// its next request, and nothing is restarted: this process serves every bot on
// the host, and restarting it for one bot's cookies would stop them all.
//
// bot_id is the containment boundary between bots: it is validated against a
// strict pattern and joined under BOTS_ROOT, so one bot can never reach
// another's session. Do not relax that regex.
// ---------------------------------------------------------------------------
const BOT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

function requireBotId(botId) {
  if (typeof botId !== 'string' || !BOT_ID_PATTERN.test(botId)) {
    throw new Error('Invalid or missing bot_id');
  }
  return botId;
}

function getBotAuthDir(botId) {
  return path.join(BOTS_ROOT, requireBotId(botId), 'youtube_auth');
}

function getBotCookiesFile(botId) {
  return path.join(getBotAuthDir(botId), 'cookies.txt');
}

function getBotSessionMetaFile(botId) {
  return path.join(getBotAuthDir(botId), 'session.json');
}

// Cache key changes with the cookie file, so signing in, a refresh that rotated
// the cookies, or signing out builds a fresh session on the next request instead
// of serving a stale one.
function sessionKey(botId) {
  if (!botId) return 'anonymous';
  try {
    const stat = statSync(getBotCookiesFile(botId));
    return `${botId}:${stat.mtimeMs}:${stat.size}`;
  } catch {
    return `${botId}:anonymous`;
  }
}

/**
 * The bot's stored browser session, or null when it has none.
 * @returns {Promise<{cookie: string, datasyncId: string}|null>}
 */
async function readBrowserSession(botId) {
  let text;
  try {
    text = await fs.readFile(getBotCookiesFile(botId), 'utf8');
  } catch (error) {
    if (error?.code !== 'ENOENT') {
      console.warn(`[youtube-bridge] Unreadable cookies for ${botId}:`, error.message);
    }
    return null;
  }
  const cookie = cookieHeaderFromNetscape(text);
  if (!cookie) return null;

  let datasyncId = '';
  try {
    const meta = JSON.parse(await fs.readFile(getBotSessionMetaFile(botId), 'utf8'));
    datasyncId = typeof meta?.datasync_id === 'string' ? meta.datasync_id : '';
  } catch {}
  return { cookie, datasyncId };
}

// A session imported on a host with no Chrome (arm64) arrives without a
// DataSync ID, because nothing loaded youtube.com to read ytcfg. YouTube also
// reports it in the account menu's response context.
async function discoverDatasyncId(session) {
  try {
    const response = await session.actions.execute('/account/account_menu', { client: 'WEB' });
    const id = response?.data?.responseContext?.mainAppWebResponseContext?.datasyncId;
    return typeof id === 'string' ? id : '';
  } catch (error) {
    console.warn('[youtube-bridge] Could not read the DataSync ID:', error?.message || error);
    return '';
  }
}

async function getSession(botId) {
  const key = sessionKey(botId);
  const cached = sessionCache.get(key);
  if (cached) {
    sessionCache.delete(key);
    sessionCache.set(key, cached);
    return cached;
  }

  for (const cachedKey of sessionCache.keys()) {
    if (cachedKey.startsWith(`${botId}:`)) sessionCache.delete(cachedKey);
  }

  const contextPromise = (async () => {
    const browserSession = botId ? await readBrowserSession(botId) : null;
    const { session, poToken } = await createAttestedSession({
      user_agent: USER_AGENT,
      client_type: ClientType.MWEB,
      cache: new UniversalCache(true, botId ? getBotAuthDir(botId) : undefined),
      enable_session_cache: !browserSession,
      generate_session_locally: true,
      retrieve_player: true,
      ...(browserSession ? { cookie: browserSession.cookie } : {})
    }, browserSession);
    return { session, poToken };
  })().catch((error) => {
    sessionCache.delete(key);
    throw error;
  });

  sessionCache.set(key, contextPromise);
  while (sessionCache.size > SESSION_CACHE_MAX_ENTRIES) {
    sessionCache.delete(sessionCache.keys().next().value);
  }
  return contextPromise;
}

async function sessionStatus(body) {
  const botId = requireBotId(body.bot_id);
  const browserSession = await readBrowserSession(botId);
  return { status: browserSession ? 'authenticated' : 'signed_out', signed_in: Boolean(browserSession) };
}

async function getSearchSession() {
  if (!searchSessionPromise) {
    searchSessionPromise = Innertube.create({
      client_type: ClientType.WEB,
      cache: new UniversalCache(true),
      enable_session_cache: true,
      generate_session_locally: true,
      retrieve_player: false,
      fail_fast: true
    }).catch((error) => {
      searchSessionPromise = null;
      throw error;
    });
  }
  return searchSessionPromise;
}

// An anonymous session that can decipher, which getSearchSession cannot
// (retrieve_player: false). This is the fallback when a signed-in session is
// what the player endpoint is rejecting — see PLAYBACK_ATTEMPTS below.
let playbackSessionPromise = null;

async function getAnonymousPlaybackSession() {
  if (!playbackSessionPromise) {
    playbackSessionPromise = createAttestedSession({
      user_agent: USER_AGENT,
      client_type: ClientType.MWEB,
      cache: new UniversalCache(true),
      enable_session_cache: true,
      generate_session_locally: true,
      retrieve_player: true
    }).catch((error) => {
      playbackSessionPromise = null;
      throw error;
    });
  }
  return playbackSessionPromise;
}

// A proof-of-origin token, bound to `contentBinding`.
//
// The binding is the whole point and was wrong. YouTube checks that the token's
// binding matches the identity of the request it arrives on; a token bound to
// anything else is not an error, it is simply ignored, and the request is then
// treated as un-attested. From a datacenter IP that means
// "LOGIN_REQUIRED: Sign in to confirm you're not a bot" on every client, whether
// signed in or not — which is exactly what it did.
//
// For youtubei.js the token is **session-bound**: `po_token` is a session option
// that flows into Session.getSessionData alongside `visitor_data`, and into
// Player.create. So the binding must be the session's own visitorData. It was
// the video ID, handed to that session-shaped slot on every call, and the
// session itself was created with neither a token nor a matching visitor_data.
//
// Cached per binding until it expires, because minting one is not free and a
// session keeps its visitorData.
const poTokenCache = new Map();

async function getPoToken(contentBinding) {
  if (!contentBinding) return undefined;

  const cached = poTokenCache.get(contentBinding);
  if (cached && cached.expiresAtMs > Date.now() + 60_000) {
    return cached.token;
  }

  try {
    const response = await fetch(POT_URL, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ content_binding: contentBinding })
    });
    if (!response.ok) {
      const body = await response.text();
      console.warn(`[youtube-bridge] POT provider HTTP ${response.status}: ${body.slice(0, 300)}`);
      return undefined;
    }
    const data = await response.json();
    const token = data.poToken || data.po_token || undefined;
    if (token) {
      const expiresAtMs = data.expiresAt
        ? Date.parse(data.expiresAt)
        : Date.now() + 30 * 60_000;
      poTokenCache.set(contentBinding, { token, expiresAtMs });
      // Bounded: one entry per session, and sessions are already capped.
      while (poTokenCache.size > 128) {
        poTokenCache.delete(poTokenCache.keys().next().value);
      }
    }
    return token;
  } catch (error) {
    // Worth being loud about. Without a token, playback fails from any IP
    // YouTube does not trust, and the failure names bot detection rather than
    // the missing provider, which sends the reader in the wrong direction.
    console.warn(
      `[youtube-bridge] POT provider at ${POT_URL} is unavailable (${error.message}). ` +
      'Playback will fail with "Sign in to confirm you\'re not a bot" on any ' +
      'address YouTube does not trust, which includes most VPS hosts.'
    );
    return undefined;
  }
}

// Create a session that carries a proof-of-origin token bound to the identity
// YouTube will check it against.
//
// Two creates, because the binding does not exist until a session does. Signed
// out, it is the session's visitorData: generate_session_locally produces that
// without a network round trip, and retrieve_player skips fetching the player,
// so the probe is cheap. Signed in with a browser session, it is the account's
// DataSync ID, read from the bot's Chrome and stored beside the cookies, or
// asked of YouTube when an imported session arrived without one. Sessions are
// cached, so this happens once per bot per cookie file.
async function createAttestedSession(options, browserSession = null) {
  const signedIn = Boolean(browserSession);
  const probe = await Innertube.create({
    ...options,
    retrieve_player: false
  });
  const visitorData = probe.session?.context?.client?.visitorData;
  let datasyncId = browserSession?.datasyncId || '';
  if (signedIn && !datasyncId) {
    datasyncId = await discoverDatasyncId(probe);
  }

  const binding = contentBindingFor({ signedIn, visitorData, datasyncId });
  if (!binding) {
    console.warn(
      signedIn
        ? '[youtube-bridge] No DataSync ID for this signed-in session, so it carries no ' +
          'proof-of-origin token and playback may be refused as bot traffic.'
        : '[youtube-bridge] No visitorData was available, so this session carries no ' +
          'proof-of-origin token and playback may be refused as bot traffic.'
    );
    return { session: await Innertube.create(options), poToken: undefined };
  }

  const poToken = await getPoToken(binding);
  if (!poToken) {
    console.warn(
      '[youtube-bridge] Continuing without a proof-of-origin token. Search will ' +
      'work; playback will likely be refused as bot traffic.'
    );
  }

  const session = await Innertube.create({
    ...options,
    ...(visitorData ? { visitor_data: visitorData } : {}),
    po_token: poToken
  });

  return { session, poToken };
}

function extractVideoId(input) {
  if (!input) return null;
  if (/^[A-Za-z0-9_-]{11}$/.test(input)) return input;
  try {
    const url = new URL(input);
    if (url.hostname === 'youtu.be') return url.pathname.split('/').filter(Boolean)[0] || null;
    if (url.searchParams.get('v')) return url.searchParams.get('v');
    const shorts = url.pathname.match(/^\/shorts\/([^/?]+)/);
    if (shorts) return shorts[1];
  } catch {}
  return null;
}

async function extractPlaylistId(input, session) {
  if (!input) return null;
  if (/^[A-Za-z0-9_-]{18,50}$/.test(input)) {
    if (input.startsWith('UC') && input.length === 24) {
      return 'UU' + input.slice(2);
    }
    return input;
  }
  try {
    const url = new URL(input);
    const listParam = url.searchParams.get('list');
    if (listParam) return listParam;

    const channelMatch = url.pathname.match(/\/channel\/(UC[A-Za-z0-9_-]{22})/);
    if (channelMatch) {
      return 'UU' + channelMatch[1].slice(2);
    }

    if (session && (url.pathname.includes('@') || url.pathname.includes('/c/') || url.pathname.includes('/user/') || url.pathname.includes('/channel/'))) {
      try {
        const res = await session.resolveURL(input);
        const browseId = res?.payload?.browseId || res?.endpoint?.payload?.browseId;
        if (browseId && browseId.startsWith('UC')) {
          return 'UU' + browseId.slice(2);
        } else if (browseId) {
          return browseId;
        }
      } catch (e) {
        console.warn(`[youtube-bridge] resolveURL failed for ${input}: ${e.message}`);
      }
    }
  } catch {}
  return null;
}

function textValue(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  if (typeof value.toString === 'function') return value.toString();
  return '';
}

function ownerName(info) {
  const basic = info?.basic_info || {};
  return textValue(basic.author) || textValue(basic.channel?.name) || '';
}

function infoPayload(info, videoId) {
  const basic = info?.basic_info || {};
  return {
    id: basic.id || videoId,
    videoId: basic.id || videoId,
    title: basic.title || '',
    uploader: ownerName(info),
    duration: basic.duration || 0,
    is_live: Boolean(basic.is_live || basic.is_live_content),
    webpage_url: `https://www.youtube.com/watch?v=${basic.id || videoId}`,
    http_headers: { 'User-Agent': USER_AGENT }
  };
}

function formatPayload(format) {
  return {
    url: format.url,
    itag: format.itag,
    mime_type: format.mime_type,
    bitrate: format.bitrate,
    average_bitrate: format.average_bitrate,
    content_length: format.content_length,
    quality: format.quality,
    quality_label: format.quality_label,
    audio_quality: format.audio_quality,
    audio_sample_rate: format.audio_sample_rate,
    audio_channels: format.audio_channels,
    has_audio: format.has_audio,
    has_video: format.has_video
  };
}

// getBasicInfo's `client` option is NOT the ClientType enum. It is validated
// against Constants.SUPPORTED_CLIENTS, which holds the enum's *keys*
// ('TV_EMBEDDED', 'WEB_EMBEDDED'), while ClientType.TV_EMBEDDED is its *value*
// ('TVHTML5_SIMPLY_EMBEDDED_PLAYER'). Passing the enum through here throws
// "Invalid client: TVHTML5_SIMPLY_EMBEDDED_PLAYER" and always has. ClientType
// is the right vocabulary for Innertube.create({ client_type }) and the wrong
// one here; the two parameters look interchangeable and are not.
async function getPlayableInfo(session, videoId, client, poToken) {
  return session.getBasicInfo(videoId, { client, po_token: poToken });
}

function playabilityDescription(info) {
  const status = info?.playability_status?.status || 'UNKNOWN';
  const reason = info?.playability_status?.reason || '';
  return reason ? `${status}: ${reason}` : status;
}

async function resolveFormat(context, videoId, requestedClient, formatOptions) {
  // planPlaybackAttempts lives in media.mjs, with the reasoning for the order
  // and a note on why the client names are not ClientType values.
  const signedIn = Boolean(context?.session?.session?.logged_in);
  const attempts = planPlaybackAttempts({ signedIn });
  const failures = [];

  for (const attempt of attempts) {
    const client = attempt.client;
    const label = attempt.label;
    const clientStartedAt = performance.now();
    try {
      // The anonymous session is built only if an attempt actually needs it, so
      // a bot whose first attempt succeeds never pays for it.
      const attemptContext = attempt.session === 'anonymous'
        ? await getAnonymousPlaybackSession()
        : context;
      const session = attemptContext.session;

      // The session's own token, minted against its visitorData when the session
      // was created. Not a fresh one bound to this video: the binding has to
      // match the identity of the request, and for youtubei.js that identity is
      // the session. Binding it to the video ID here is what left every request
      // effectively un-attested.
      const poToken = clientTakesPoToken(client) ? attemptContext.poToken : undefined;
      console.log(`[youtube-bridge-timing] video=${videoId} client=${label} stage=po-token available=${Boolean(poToken)}`);

      const playerStartedAt = performance.now();
      const info = await getPlayableInfo(session, videoId, client, poToken);
      console.log(`[youtube-bridge-timing] video=${videoId} client=${label} stage=player elapsed_ms=${Math.round(performance.now() - playerStartedAt)} status=${info?.playability_status?.status || 'UNKNOWN'}`);
      if (!info?.streaming_data) {
        throw new Error(`no streaming data (${playabilityDescription(info)})`);
      }

      const formatStartedAt = performance.now();
      const format = info.chooseFormat(formatOptions);
      console.log(`[youtube-bridge-timing] video=${videoId} client=${label} stage=choose-format elapsed_ms=${Math.round(performance.now() - formatStartedAt)} itag=${format.itag}`);
      if (!session.session.player) {
        throw new Error('YouTube player is unavailable');
      }

      session.session.player.po_token = poToken;
      const decipherStartedAt = performance.now();
      format.url = await format.decipher(session.session.player);
      console.log(`[youtube-bridge-timing] video=${videoId} client=${label} stage=decipher elapsed_ms=${Math.round(performance.now() - decipherStartedAt)}`);

      if (!format.url) {
        throw new Error('decipher returned an empty stream URL');
      }

      console.log(`[youtube-bridge] resolved ${videoId} with client=${label} itag=${format.itag} elapsed_ms=${Math.round(performance.now() - clientStartedAt)}`);
      return { info, format, client };
    } catch (error) {
      const message = error?.message || String(error);
      failures.push(`${label}: ${message}`);
      console.warn(`[youtube-bridge] ${videoId} client=${label} failed: ${message}`);
    }
  }

  throw new Error(`Unable to resolve stream for ${videoId}; ${failures.join(' | ')}`);
}

async function resolveTrack(body) {
  const startedAt = performance.now();
  const videoId = body.video_id || extractVideoId(body.url);
  if (!videoId) throw new Error('Invalid YouTube URL or video ID');
  const requestedClient = body.client === 'YTMUSIC' ? 'YTMUSIC' : 'MWEB';
  const cacheKey = `${sessionKey(body.bot_id)}:${requestedClient}:${videoId}`;
  const cached = resolveCache.get(cacheKey);
  if (cached) {
    console.log(`[youtube-bridge] resolve cache hit ${videoId} client=${requestedClient} elapsed_ms=${Math.round(performance.now() - startedAt)} cache_entries=${resolveCache.size}`);
    return cached;
  }
  console.log(`[youtube-bridge] resolve cache miss ${videoId} client=${requestedClient} cache_entries=${resolveCache.size}`);

  const pending = pendingResolutions.get(cacheKey);
  if (pending) {
    console.log(`[youtube-bridge] joining pending resolve ${videoId} client=${requestedClient} pending=${pendingResolutions.size}`);
    return pending;
  }

  const resolution = resolveTrackUncached(body, videoId, requestedClient)
    .then((payload) => {
      const ttlMs = streamCacheTtlMs(payload.url);
      const cachedPayload = {
        ...payload,
        cache_expires_at_ms: Date.now() + ttlMs
      };
      resolveCache.set(cacheKey, cachedPayload, ttlMs);
      scheduleSaveDiskCache();
      console.log(`[youtube-bridge] resolve completed ${videoId} client=${requestedClient} elapsed_ms=${Math.round(performance.now() - startedAt)} ttl_ms=${Math.round(ttlMs)} cache_entries=${resolveCache.size}`);
      return cachedPayload;
    })
    .finally(() => pendingResolutions.delete(cacheKey));
  pendingResolutions.set(cacheKey, resolution);
  return resolution;
}

function invalidateResolution(body) {
  const videoId = body.video_id || extractVideoId(body.url);
  if (!videoId) throw new Error('Invalid YouTube URL or video ID');
  const requestedClient = body.client === 'YTMUSIC' ? 'YTMUSIC' : 'MWEB';
  const cacheKey = `${sessionKey(body.bot_id)}:${requestedClient}:${videoId}`;
  resolveCache.delete(cacheKey);
  pendingResolutions.delete(cacheKey);
  console.log(`[youtube-bridge] resolve cache invalidated ${videoId} client=${requestedClient}`);
  return { invalidated: true };
}

async function resolveTrackUncached(body, videoId, requestedClient) {
  const context = await getSession(body.bot_id);

  const { info, format, client } = await resolveFormat(context, videoId, requestedClient, {
    type: 'audio',
    quality: 'best',
    format: 'any'
  });

  const metadata = infoPayload(info, videoId);
  return {
    ...metadata,
    ...formatPayload(format),
    client,
    format: 'mp3',
    http_headers: { 'User-Agent': USER_AGENT }
  };
}

async function getInfo(body) {
  const videoId = body.video_id || extractVideoId(body.url);
  if (!videoId) throw new Error('Invalid YouTube URL or video ID');
  const context = await getSession(body.bot_id);
  const client = body.client === 'YTMUSIC' ? 'YTMUSIC' : 'MWEB';
  // The session's token, for the same reason as in resolveFormat: it is bound to
  // the session's visitorData, and a token bound to this video would be ignored.
  const info = await getPlayableInfo(context.session, videoId, client, context.poToken);
  return {
    ...infoPayload(info, videoId),
    playability_status: info?.playability_status?.status || '',
    playability_reason: info?.playability_status?.reason || ''
  };
}

async function getWebSession(botId) {
  // Signed in with the bot's browser session, so the account's private
  // playlists resolve too; public ones resolve either way.
  const browserSession = botId ? await readBrowserSession(botId) : null;
  return Innertube.create({
    user_agent: USER_AGENT,
    client_type: ClientType.WEB,
    cache: new UniversalCache(true, botId ? getBotAuthDir(botId) : undefined),
    enable_session_cache: !browserSession,
    generate_session_locally: true,
    retrieve_player: false,
    ...(browserSession ? { cookie: browserSession.cookie } : {})
  });
}

async function getPlaylist(body) {
  const session = await getWebSession(body.bot_id);
  const playlistId = body.playlist_id || (await extractPlaylistId(body.url, session));
  if (!playlistId) throw new Error('Invalid YouTube playlist URL or ID');
  const playlist = await session.getPlaylist(playlistId);
  const allItems = [...(playlist?.items || playlist?.videos || [])];

  let current = playlist;
  while (current?.has_continuation) {
    try {
      current = await current.getContinuation();
      const pageItems = current?.items || current?.videos || [];
      if (!pageItems.length) break;
      allItems.push(...pageItems);
    } catch (error) {
      console.warn(`[youtube-bridge] Playlist pagination completed or stopped: ${error.message}`);
      break;
    }
  }

  console.log(`[youtube-bridge] playlist ${playlistId} fetched ${allItems.length} total items across all pages`);

  const defaultUploader = textValue(playlist?.info?.author?.name) || textValue(playlist?.info?.title) || '';

  return {
    id: playlistId,
    title: textValue(playlist?.info?.title),
    uploader: defaultUploader,
    entries: allItems.map((item) => {
      let id = item.id || item.video_id || item.content_id;
      if (!id || id.length !== 11) {
        const str = JSON.stringify(item);
        const match = str.match(/"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"/);
        if (match) id = match[1];
      }
      const title = textValue(item.title?.text || item.title || item.metadata?.title?.text || item.metadata?.title);
      const uploader = textValue(item.author?.name || item.author?.text || item.author || item.metadata?.author?.name || item.short_by_line_text) || defaultUploader;
      return {
        id,
        videoId: id,
        title,
        uploader,
        webpage_url: id ? `https://www.youtube.com/watch?v=${id}` : ''
      };
    }).filter((item) => item.id && item.id.length === 11)
  };
}

async function searchVideos(body) {
  const query = String(body.query || '').trim();
  if (!query) throw new Error('Search query is required');
  const limit = Math.min(Math.max(Number(body.limit) || 10, 1), 50);
  const mode = body.mode === 'music' ? 'music' : 'video';
  const startedAt = performance.now();
  const cacheKey = normalizeSearchKey(mode, query);
  const cached = searchCache.get(cacheKey);
  if (cached) {
    console.log(`[youtube-bridge] search cache hit mode=${mode} query="${query}" elapsed_ms=${Math.round(performance.now() - startedAt)}`);
    return { entries: cached.slice(0, limit) };
  }

  const entries = await searchCache.getOrCreate(cacheKey, SEARCH_CACHE_TTL_MS, async () => {
    const session = await getSearchSession();
    try {
      if (mode === 'music') {
        const rawRes = await session.actions.execute('/search', {
          query,
          params: 'Eg-KAQwIARAAGAAgACgAMABqChAEEAMQCRAFEAo%3D',
          client: 'YTMUSIC'
        });
        const tab = rawRes.data?.contents?.tabbedSearchResultsRenderer?.tabs?.[0]?.tabRenderer;
        const sections = tab?.content?.sectionListRenderer?.contents || [];
        const ytmEntries = [];
        for (const sec of sections) {
          const items = sec?.musicShelfRenderer?.contents || sec?.musicCardShelfRenderer?.contents || [];
          for (const item of items) {
            const r = item?.musicResponsiveListItemRenderer;
            if (!r) continue;
            const flexCols = r.flexColumns || [];
            const title = flexCols[0]?.musicResponsiveListItemFlexColumnRenderer?.text?.runs?.[0]?.text;
            const artist = flexCols[1]?.musicResponsiveListItemFlexColumnRenderer?.text?.runs?.[0]?.text;
            const videoId = r.playlistItemData?.videoId || r.overlay?.musicItemThumbnailOverlayRenderer?.content?.musicPlayButtonRenderer?.playNavigationEndpoint?.watchEndpoint?.videoId;
            if (videoId) {
              ytmEntries.push({
                id: videoId,
                videoId,
                title: title || 'Unknown Title',
                uploader: artist || '',
                webpage_url: `https://www.youtube.com/watch?v=${videoId}`
              });
            }
          }
        }
        if (ytmEntries.length > 0) return ytmEntries.slice(0, 50);
      } else {
        const rawRes = await session.actions.execute('/search', {
          query,
          params: 'EgIQAQ%3D%3D',
          client: 'WEB'
        });
        const ytContents = rawRes.data?.contents?.twoColumnSearchResultsRenderer?.primaryContents?.sectionListRenderer?.contents || [];
        const ytEntries = [];
        for (const sec of ytContents) {
          const items = sec?.itemSectionRenderer?.contents || [];
          for (const item of items) {
            const v = item?.videoRenderer;
            if (!v || !v.videoId) continue;
            const title = v.title?.runs?.[0]?.text || v.title?.simpleText;
            const uploader = v.ownerText?.runs?.[0]?.text || v.shortBylineText?.runs?.[0]?.text;
            ytEntries.push({
              id: v.videoId,
              videoId: v.videoId,
              title: title || 'Unknown Title',
              uploader: uploader || '',
              webpage_url: `https://www.youtube.com/watch?v=${v.videoId}`
            });
          }
        }
        if (ytEntries.length > 0) return ytEntries.slice(0, 50);
      }
    } catch (rawErr) {
      console.warn(`[youtube-bridge] Fast raw search failed for "${query}", falling back to parser:`, rawErr.message);
    }

    if (mode === 'music') {
      const search = await session.music.search(query, { type: 'song' });
      const items = search?.songs?.contents || search?.contents || [];
      return items
        .map(musicItemPayload)
        .filter(Boolean)
        .slice(0, 50);
    }
    const search = await session.search(query, { type: 'video' });
    const items = search?.videos || search?.results || search?.contents || [];
    return items.slice(0, 50).map((video) => ({
      id: video.id || video.videoId,
      videoId: video.id || video.videoId,
      title: textValue(video.title),
      uploader: textValue(video.author?.name || video.author?.text || video.author),
      webpage_url: (video.id || video.videoId) ? `https://www.youtube.com/watch?v=${video.id || video.videoId}` : ''
    })).filter((video) => video.id);
  });
  console.log(`[youtube-bridge] searched mode=${mode} query="${query}" in ${Math.round(performance.now() - startedAt)}ms cache_entries=${searchCache.size}`);
  scheduleSaveDiskCache();

  const results = entries.slice(0, limit);
  if (limit === 1 && results.length > 0) {
    const topVideoId = results[0].videoId || results[0].id;
    if (topVideoId) {
      const requestedClient = mode === 'music' ? 'YTMUSIC' : 'MWEB';
      const cacheKey = `${sessionKey(body.bot_id)}:${requestedClient}:${topVideoId}`;
      const cached = resolveCache.get(cacheKey);
      if (cached && cached.url) {
        results[0] = {
          ...results[0],
          ...cached,
          stream_url: cached.url
        };
      } else {
        try {
          const resolved = await resolveTrack({
            video_id: topVideoId,
            client: requestedClient,
            bot_id: body.bot_id
          });
          if (resolved && resolved.url) {
            results[0] = {
              ...results[0],
              ...resolved,
              stream_url: resolved.url
            };
          }
        } catch (err) {
          console.warn(`[youtube-bridge] pre-resolve in search failed for ${topVideoId}: ${err.message}`);
        }
      }
    }
  }

  return { entries: results };
}

async function getMusicRecommendations(body) {
  const videoId = body.video_id || extractVideoId(body.url);
  if (!videoId) throw new Error('Invalid YouTube URL or video ID');
  const limit = Math.min(Math.max(Number(body.limit) || 20, 1), 50);
  const cacheKey = `${sessionKey(body.bot_id)}:${videoId}`;
  const startedAt = performance.now();
  const cached = recommendationCache.get(cacheKey);
  if (cached) {
    console.log(`[youtube-bridge] recommendations cache hit ${videoId} elapsed_ms=${Math.round(performance.now() - startedAt)}`);
    return { entries: cached.slice(0, limit) };
  }

  const entries = await recommendationCache.getOrCreate(
    cacheKey,
    RECOMMENDATION_CACHE_TTL_MS,
    async () => {
      const { session } = await getSession(body.bot_id);
      try {
        const playlist = await session.music.getUpNext(videoId, true);
        return (playlist?.contents || [])
          .map(musicItemPayload)
          .filter((item) => item && item.videoId !== videoId)
          .slice(0, 50);
      } catch (err) {
        console.warn(`[youtube-bridge] getUpNext(automix) failed for ${videoId}: ${err.message}. Retrying standard upNext...`);
        try {
          const fallbackPlaylist = await session.music.getUpNext(videoId, false);
          return (fallbackPlaylist?.contents || [])
            .map(musicItemPayload)
            .filter((item) => item && item.videoId !== videoId)
            .slice(0, 50);
        } catch (fallbackErr) {
          console.warn(`[youtube-bridge] getUpNext fallback failed for ${videoId}: ${fallbackErr.message}`);
          return [];
        }
      }
    }
  );
  console.log(`[youtube-bridge] recommendations completed ${videoId} elapsed_ms=${Math.round(performance.now() - startedAt)} cache_entries=${recommendationCache.size}`);
  return { entries: entries.slice(0, limit) };
}

async function getDownloadPlan(body) {
  const videoId = body.video_id || extractVideoId(body.url);
  if (!videoId) throw new Error('Invalid YouTube URL or video ID');
  const context = await getSession(body.bot_id);
  const requestedClient = body.client === 'YTMUSIC' ? 'YTMUSIC' : 'MWEB';

  if (!body.video) {
    const { format: audio, client } = await resolveFormat(context, videoId, requestedClient, {
      type: 'audio', quality: 'best', format: 'any'
    });
    return {
      audio: formatPayload(audio),
      client,
      http_headers: { 'User-Agent': USER_AGENT }
    };
  }

  let videoResult;
  try {
    videoResult = await resolveFormat(context, videoId, requestedClient, {
      type: 'video', quality: 'best', format: 'mp4'
    });
  } catch {
    videoResult = await resolveFormat(context, videoId, requestedClient, {
      type: 'video', quality: 'best', format: 'any'
    });
  }

  let audioResult;
  try {
    audioResult = await resolveFormat(context, videoId, requestedClient, {
      type: 'audio', quality: 'best', format: 'mp4'
    });
  } catch {
    audioResult = await resolveFormat(context, videoId, requestedClient, {
      type: 'audio', quality: 'best', format: 'any'
    });
  }

  return {
    video: formatPayload(videoResult.format),
    audio: formatPayload(audioResult.format),
    client: `${videoResult.client}/${audioResult.client}`,
    http_headers: { 'User-Agent': USER_AGENT }
  };
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/health') {
      // The proof-of-origin provider is reported here because without it
      // playback fails as "Sign in to confirm you're not a bot", which reads
      // like an account problem and is not one. Naming it turns a day of
      // guessing into one request.
      let potReachable = false;
      let potDetail = '';
      try {
        const ping = await fetch(POT_URL.replace(/\/get_pot$/, '/ping'), {
          signal: AbortSignal.timeout(3000)
        });
        potReachable = ping.ok;
        if (!ping.ok) potDetail = `HTTP ${ping.status}`;
      } catch (error) {
        potDetail = error.message;
      }
      return json(res, 200, {
        ok: true,
        version: '3',
        pot_provider: {
          url: POT_URL,
          reachable: potReachable,
          detail: potDetail,
          note: potReachable
            ? ''
            : 'Playback will be refused as bot traffic on any address YouTube does not trust.'
        }
      });
    }
    if (req.method !== 'POST') return json(res, 404, { error: 'Not found' });

    const body = await readBody(req);
    // Validated up front for every route: bot_id is the boundary that keeps one
    // bot out of another's tokens.
    requireBotId(body.bot_id);

    // /auth/start and /auth/signout were the device-code sign-in, retired in
    // Phase 9. Sign-in and sign-out now happen in the bot, which owns the files.
    if (req.url === '/auth/status') return json(res, 200, await sessionStatus(body));

    if (req.url === '/resolve') return json(res, 200, await resolveTrack(body));
    if (req.url === '/invalidate') return json(res, 200, invalidateResolution(body));
    if (req.url === '/info') return json(res, 200, await getInfo(body));
    if (req.url === '/playlist') return json(res, 200, await getPlaylist(body));
    if (req.url === '/search') return json(res, 200, await searchVideos(body));
    if (req.url === '/recommendations') return json(res, 200, await getMusicRecommendations(body));
    if (req.url === '/download-plan') return json(res, 200, await getDownloadPlan(body));
    return json(res, 404, { error: 'Not found' });
  } catch (error) {
    console.error('[youtube-bridge]', error);
    return json(res, 500, { error: error?.message || String(error) });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[youtube-bridge] listening on http://${HOST}:${PORT}`);
  loadDiskCache().catch(() => {});
  // Background pre-warming for search session so first user request is instant
  getSearchSession().catch((err) => {
    console.warn('[youtube-bridge] Background search session warmup failed:', err?.message || err);
  });

  // Keep search socket connections warm periodically
  setInterval(async () => {
    try {
      const session = await getSearchSession();
      await session.music.search('ping', { type: 'song' }).catch(() => {});
    } catch {}
  }, 45000).unref();
});
