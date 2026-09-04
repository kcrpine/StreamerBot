import http from 'node:http';
import fs from 'node:fs/promises';
import { statSync } from 'node:fs';
import path from 'node:path';
import { URL } from 'node:url';
import { ClientType, Innertube, UniversalCache, Platform } from 'youtubei.js';
import { ExpiringLruCache } from './cache.mjs';
import { musicItemPayload, normalizeSearchKey, streamCacheTtlMs } from './media.mjs';

const HOST = process.env.YOUTUBE_BRIDGE_HOST || '127.0.0.1';
const PORT = Number(process.env.YOUTUBE_BRIDGE_PORT || 4417);
const POT_URL = process.env.POT_PROVIDER_URL || 'http://127.0.0.1:4416/get_pot';
const BOTS_ROOT = path.resolve(process.env.STREAMERBOT_BOTS_ROOT || '/bots');
const USER_AGENT = process.env.YOUTUBE_BRIDGE_USER_AGENT ||
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 Version/18.5 Mobile/15E148 Safari/604.1';

// YouTube.js 18 requires an evaluator to decipher player signatures/nsig.
Platform.shim.eval = async (data) => new Function(data.output)();

// This process is shared infrastructure: every bot on the host resolves through
// it. Node makes an unhandled rejection fatal, and the OAuth device flow polls
// Google in the background long after /auth/start has returned, so a single
// bot's abandoned or expired sign-in would otherwise take YouTube down for
// everyone. Log and keep serving instead.
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
// Per-bot OAuth credentials.
//
// Replaces the Netscape cookies.txt path entirely. Cookies expired every few
// weeks and had to be re-exported from a desktop browser by hand, which is a
// miserable thing to ask of a screen reader user. youtubei.js signs in with a
// TV device code instead and refreshes itself indefinitely.
//
// bot_id is the containment boundary between bots: it is validated against a
// strict pattern and joined under BOTS_ROOT, so one bot can never reach
// another's tokens. Do not relax that regex.
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

function getBotCredentialsFile(botId) {
  return path.join(getBotAuthDir(botId), 'credentials.json');
}

// Cache key changes with the credentials file, so signing in or out builds a
// fresh session on the next request instead of serving a stale one.
function sessionKey(botId) {
  if (!botId) return 'anonymous';
  try {
    const stat = statSync(getBotCredentialsFile(botId));
    return `${botId}:${stat.mtimeMs}:${stat.size}`;
  } catch {
    return `${botId}:anonymous`;
  }
}

async function readCredentials(botId) {
  try {
    return JSON.parse(await fs.readFile(getBotCredentialsFile(botId), 'utf8'));
  } catch (error) {
    if (error?.code === 'ENOENT') return null;
    console.warn(`[youtube-bridge] Unreadable credentials for ${botId}:`, error.message);
    return null;
  }
}

async function writeCredentials(botId, credentials) {
  const dir = getBotAuthDir(botId);
  await fs.mkdir(dir, { recursive: true });
  const file = getBotCredentialsFile(botId);
  // Written via a temp file so a crash mid-write cannot leave a truncated
  // credentials file that silently signs the bot out.
  const tmp = `${file}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(credentials), { mode: 0o600 });
  await fs.rename(tmp, file);
}

async function clearCredentials(botId) {
  await fs.rm(getBotAuthDir(botId), { recursive: true, force: true });
}

/** Attach the listener that keeps refreshed tokens on disk. */
function persistCredentialUpdates(session, botId) {
  session.on('update-credentials', async ({ credentials }) => {
    try {
      await writeCredentials(botId, credentials);
    } catch (error) {
      console.warn(`[youtube-bridge] Could not persist credentials for ${botId}:`, error.message);
    }
  });
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
    const credentials = botId ? await readCredentials(botId) : null;
    const session = await Innertube.create({
      user_agent: USER_AGENT,
      client_type: ClientType.MWEB,
      cache: new UniversalCache(true, botId ? getBotAuthDir(botId) : undefined),
      enable_session_cache: true,
      generate_session_locally: true,
      retrieve_player: true
    });
    if (credentials) {
      persistCredentialUpdates(session.session, botId);
      try {
        // Silent: with stored credentials this refreshes rather than starting
        // a device flow.
        await session.session.signIn(credentials);
      } catch (error) {
        // A revoked or corrupt grant must not take search down with it. Fall
        // back to anonymous, which is what the cookie-less path always did.
        console.warn(`[youtube-bridge] Sign-in failed for ${botId}, continuing anonymously:`, error.message);
      }
    }
    return { session };
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

// ---------------------------------------------------------------------------
// Device-code sign-in.
//
// signIn() with no credentials does not return until the user has finished on
// google.com/device, so /auth/start must not await it. It kicks the flow off,
// resolves as soon as the auth-pending event carries the code, and leaves the
// promise running in the background to complete the sign-in.
// ---------------------------------------------------------------------------
const pendingAuth = new Map();

const AUTH_PENDING_TIMEOUT_MS = 15000;

async function startAuth(body) {
  const botId = requireBotId(body.bot_id);

  const existing = pendingAuth.get(botId);
  if (existing && existing.expires_at > Date.now()) {
    return { ...existing.info, already_pending: true };
  }

  const inner = await Innertube.create({
    user_agent: USER_AGENT,
    client_type: ClientType.MWEB,
    cache: new UniversalCache(true, getBotAuthDir(botId)),
    generate_session_locally: true,
    retrieve_player: false
  });
  const session = inner.session;

  const pending = new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error('YouTube did not return a device code in time')),
      AUTH_PENDING_TIMEOUT_MS
    );
    session.once('auth-pending', (data) => {
      clearTimeout(timer);
      resolve({
        verification_url: data.verification_url,
        user_code: data.user_code,
        expires_in: data.expires_in
      });
    });
    session.once('auth-error', (error) => {
      clearTimeout(timer);
      reject(error instanceof Error ? error : new Error(String(error)));
    });
  });

  session.once('auth', async ({ credentials }) => {
    try {
      await writeCredentials(botId, credentials);
      // Drop cached anonymous sessions so the next request is signed in.
      for (const cachedKey of [...sessionCache.keys()]) {
        if (cachedKey.startsWith(`${botId}:`)) sessionCache.delete(cachedKey);
      }
      const entry = pendingAuth.get(botId);
      if (entry) entry.status = 'authenticated';
      console.log(`[youtube-bridge] ${botId} signed in to YouTube`);
    } catch (error) {
      console.error(`[youtube-bridge] Could not save credentials for ${botId}:`, error.message);
      const entry = pendingAuth.get(botId);
      if (entry) { entry.status = 'failed'; entry.error = error.message; }
    }
  });
  persistCredentialUpdates(session, botId);

  // Deliberately not awaited: it settles when the user finishes on the device
  // page, which may be minutes from now.
  session.signIn().catch((error) => {
    console.warn(`[youtube-bridge] Device sign-in for ${botId} ended:`, error?.message || error);
    const entry = pendingAuth.get(botId);
    if (entry && entry.status === 'pending') { entry.status = 'failed'; entry.error = error?.message; }
  });

  const info = await pending;
  pendingAuth.set(botId, {
    info,
    status: 'pending',
    expires_at: Date.now() + (Number(info.expires_in) || 1800) * 1000
  });
  return info;
}

async function authStatus(body) {
  const botId = requireBotId(body.bot_id);
  const credentials = await readCredentials(botId);
  if (credentials) {
    pendingAuth.delete(botId);
    return { status: 'authenticated', signed_in: true };
  }
  const entry = pendingAuth.get(botId);
  if (entry && entry.expires_at > Date.now()) {
    return {
      status: entry.status,
      signed_in: false,
      error: entry.error,
      ...entry.info
    };
  }
  pendingAuth.delete(botId);
  return { status: 'signed_out', signed_in: false };
}

async function signOut(body) {
  const botId = requireBotId(body.bot_id);
  pendingAuth.delete(botId);
  await clearCredentials(botId);
  for (const cachedKey of [...sessionCache.keys()]) {
    if (cachedKey.startsWith(`${botId}:`)) sessionCache.delete(cachedKey);
  }
  console.log(`[youtube-bridge] ${botId} signed out of YouTube`);
  return { status: 'signed_out', signed_in: false };
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

async function getPoToken(contentBinding) {
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
    return data.poToken || data.po_token || undefined;
  } catch (error) {
    console.warn(`[youtube-bridge] POT provider unavailable: ${error.message}`);
    return undefined;
  }
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

async function getPlayableInfo(session, videoId, client, poToken) {
  const targetClient = client === 'YTMUSIC' ? ClientType.MWEB : client;
  return session.getBasicInfo(videoId, { client: targetClient, po_token: poToken });
}

function playabilityDescription(info) {
  const status = info?.playability_status?.status || 'UNKNOWN';
  const reason = info?.playability_status?.reason || '';
  return reason ? `${status}: ${reason}` : status;
}

async function resolveFormat(context, videoId, requestedClient, formatOptions) {
  // WEB is SABR-only for many videos in 2026. MWEB still exposes classic
  // adaptive formats and is the preferred web playback client here.
  const clients = requestedClient === 'YTMUSIC'
    ? ['YTMUSIC', 'MWEB', ClientType.TV_EMBEDDED]
    : ['MWEB', ClientType.TV_EMBEDDED];
  const failures = [];
  const { session } = context;

  for (const client of clients) {
    const clientStartedAt = performance.now();
    try {
      const poStartedAt = performance.now();
      const poToken = client === ClientType.TV_EMBEDDED
        ? undefined
        : await getPoToken(videoId);
      console.log(`[youtube-bridge-timing] video=${videoId} client=${client} stage=po-token elapsed_ms=${Math.round(performance.now() - poStartedAt)} available=${Boolean(poToken)}`);

      const playerStartedAt = performance.now();
      const info = await getPlayableInfo(session, videoId, client, poToken);
      console.log(`[youtube-bridge-timing] video=${videoId} client=${client} stage=player elapsed_ms=${Math.round(performance.now() - playerStartedAt)} status=${info?.playability_status?.status || 'UNKNOWN'}`);
      if (!info?.streaming_data) {
        throw new Error(`no streaming data (${playabilityDescription(info)})`);
      }

      const formatStartedAt = performance.now();
      const format = info.chooseFormat(formatOptions);
      console.log(`[youtube-bridge-timing] video=${videoId} client=${client} stage=choose-format elapsed_ms=${Math.round(performance.now() - formatStartedAt)} itag=${format.itag}`);
      if (!session.session.player) {
        throw new Error('YouTube player is unavailable');
      }

      session.session.player.po_token = poToken;
      const decipherStartedAt = performance.now();
      format.url = await format.decipher(session.session.player);
      console.log(`[youtube-bridge-timing] video=${videoId} client=${client} stage=decipher elapsed_ms=${Math.round(performance.now() - decipherStartedAt)}`);

      if (!format.url) {
        throw new Error('decipher returned an empty stream URL');
      }

      console.log(`[youtube-bridge] resolved ${videoId} with client=${client} itag=${format.itag} elapsed_ms=${Math.round(performance.now() - clientStartedAt)}`);
      return { info, format, client };
    } catch (error) {
      const message = error?.message || String(error);
      failures.push(`${client}: ${message}`);
      console.warn(`[youtube-bridge] ${videoId} client=${client} failed: ${message}`);
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
  const poToken = await getPoToken(context.poBinding || videoId);
  const info = await getPlayableInfo(context.session, videoId, client, poToken);
  return {
    ...infoPayload(info, videoId),
    playability_status: info?.playability_status?.status || '',
    playability_reason: info?.playability_status?.reason || ''
  };
}

async function getWebSession(botId) {
  const credentials = botId ? await readCredentials(botId) : null;
  const inner = await Innertube.create({
    user_agent: USER_AGENT,
    client_type: ClientType.WEB,
    cache: new UniversalCache(true, botId ? getBotAuthDir(botId) : undefined),
    enable_session_cache: true,
    generate_session_locally: true,
    retrieve_player: false
  });
  if (credentials) {
    persistCredentialUpdates(inner.session, botId);
    try {
      await inner.session.signIn(credentials);
    } catch (error) {
      // Private playlists will not be visible, but public ones still resolve.
      console.warn(`[youtube-bridge] Playlist sign-in failed for ${botId}:`, error.message);
    }
  }
  return inner;
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
      return json(res, 200, { ok: true, version: '2' });
    }
    if (req.method !== 'POST') return json(res, 404, { error: 'Not found' });

    const body = await readBody(req);
    // Validated up front for every route: bot_id is the boundary that keeps one
    // bot out of another's tokens.
    requireBotId(body.bot_id);

    if (req.url === '/auth/start') return json(res, 200, await startAuth(body));
    if (req.url === '/auth/status') return json(res, 200, await authStatus(body));
    if (req.url === '/auth/signout') return json(res, 200, await signOut(body));

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
