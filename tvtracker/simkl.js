// Simkl client for movies and TV shows. Replaces the old AniList client.
// Anime is left out on purpose: Simkl files it under its own "anime" type,
// and every search and activity lookup here skips it.
//
// Endpoints (from Simkl's public API docs):
//   search:    GET  api.simkl.org/search/{movie|tv|all}?q=…
//   details:   GET  api.simkl.com/{movies|tv}/{id}
//   activity:  GET  api.simkl.com/sync/activities           (needs a user token)
//   watched:   GET  api.simkl.com/sync/all-items/{movies|shows} (needs a user token)
//   sign-in:   POST api.simkl.com/oauth2/device, then /oauth2/token (device code, no redirect URL)
//   account:   GET  api.simkl.com/users/settings            (needs a user token)
//   renew:     POST api.simkl.com/oauth2/token (refresh_token grant; sign-ins last 7 days)

const SIMKL_API = 'https://api.simkl.com';
const SIMKL_SEARCH = 'https://api.simkl.org';
const SIMKL_DATA = 'https://data.simkl.in'; // pre-built trending lists
// Simkl serves .webp images and recommends the wsrv.nl image proxy. Asking the
// proxy for JPG keeps them readable by Jimp (embed colours, painted banners).
const IMAGE_PROXY = 'https://wsrv.nl/?url=simkl.in';
const REQUEST_TIMEOUT_MS = 12000;
const APP_NAME = 'tvtracker'; // Simkl asks for a short lowercase app name.
const APP_VERSION = '1.0.0';
const USER_AGENT = `${APP_NAME}/${APP_VERSION}`;
const DEVICE_SCOPE = 'media:read media:write';

class SimklError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

function clientId() {
  return process.env.SIMKL_CLIENT_ID || '';
}

function isConfigured() {
  return Boolean(clientId());
}

function commonQuery(extra = {}) {
  return new URLSearchParams({
    client_id: clientId(),
    'app-name': APP_NAME,
    'app-version': APP_VERSION,
    ...extra,
  }).toString();
}

// One request, never throwing on a normal "no" answer. A 429 is retried once
// after the wait Simkl asks for. Errors from the network come back as SimklError.
async function request(url, { method = 'GET', token, form, json, retryOn429 = true } = {}) {
  const headers = { 'User-Agent': USER_AGENT, Accept: 'application/json', 'simkl-api-key': clientId() };
  if (token) headers.Authorization = `Bearer ${token}`;
  let body;
  if (form) {
    headers['Content-Type'] = 'application/x-www-form-urlencoded';
    body = new URLSearchParams(form).toString();
  } else if (json) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  }

  let res;
  try {
    res = await fetch(url, { method, headers, body, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
  } catch (err) {
    throw new SimklError(`Simkl didn't answer (${err.name === 'TimeoutError' ? 'timed out' : err.message})`);
  }

  if (res.status === 429 && retryOn429) {
    const wait = Math.min(parseInt(res.headers.get('retry-after'), 10) || 10, 60);
    await new Promise((r) => setTimeout(r, wait * 1000));
    return request(url, { method, token, form, json, retryOn429: false });
  }

  const text = await res.text().catch(() => '');
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  return { status: res.status, ok: res.ok, data };
}

function imageUrl(folder, path, size) {
  if (!path) return null;
  if (/^https?:\/\//.test(path)) return path;
  return `${IMAGE_PROXY}/${folder}/${path}${size}.webp&output=jpg&q=88`;
}

// Poster sizes: _m 340 wide (default), _ca 190 wide, _cm 84 wide.
function posterUrl(poster, size = '_m') {
  return imageUrl('posters', poster, size);
}

// Backdrop sizes: _mobile 960x540 (default), _w 600x338, _medium 1920x1080.
function fanartUrl(fanart, size = '_mobile') {
  return imageUrl('fanart', fanart, size);
}

function trailersOf(raw) {
  return (Array.isArray(raw.trailers) ? raw.trailers : [])
    .map((t) => ({ name: t.name || 'Trailer', youtube: t.youtube || t.youtube_id || null }))
    .filter((t) => t.youtube);
}

// Movies and shows keep their Simkl type under endpoint_type ("movies", "tv",
// "anime"). Returns null for anime and anything unrecognised.
function kindOf(raw) {
  const type = raw.endpoint_type || raw.type;
  if (type === 'movies' || type === 'movie') return 'movie';
  if (type === 'tv' || type === 'shows' || type === 'show') return 'show';
  return null;
}

function simklRating(ratings) {
  const value = ratings?.simkl?.rating ?? ratings?.simkl;
  return typeof value === 'number' ? value : null;
}

// One consistent shape for a title, whatever Simkl returned.
function normalizeTitle(raw, forcedKind = null) {
  const kind = forcedKind || kindOf(raw);
  if (!kind) return null;
  const ids = raw.ids || {};
  // Trending lists give a site path ("/movies/123/dune") instead of ids.
  const fromUrl = String(raw.url || '').match(/^\/(?:movies|tv|anime)\/(\d+)(?:\/([^/?#]+))?/);
  const id = ids.simkl_id ?? ids.simkl ?? raw.id ?? (fromUrl ? Number(fromUrl[1]) : null);
  if (!id) return null;
  const slug = ids.slug || (fromUrl && fromUrl[2]) || String(id);
  const released = String(raw.release_date || '').match(/(\d{4})$/);
  return {
    key: `${kind}_${id}`,
    kind,
    id: Number(id),
    title: raw.title || 'Untitled',
    year: raw.year || (released ? Number(released[1]) : null),
    poster: posterUrl(raw.poster),
    posterSmall: posterUrl(raw.poster, '_ca'),
    fanart: fanartUrl(raw.fanart),
    siteUrl: `https://simkl.com/${kind === 'movie' ? 'movies' : 'tv'}/${id}/${slug}`,
    imdb: ids.imdb || null,
    rating: simklRating(raw.ratings),
    overview: raw.overview || null,
    genres: Array.isArray(raw.genres) ? raw.genres : [],
    status: raw.status || null,
    episodes: raw.ep_count ?? raw.total_episodes ?? raw.total_episodes_count ?? null,
    runtime: Number(raw.runtime) || null,
    network: raw.network || null,
    trailers: trailersOf(raw),
  };
}

async function searchTitles(text, kind = 'all', limit = 5) {
  const type = { movie: 'movie', show: 'tv' }[kind] || 'all';
  const url = `${SIMKL_SEARCH}/search/${type}?${commonQuery({ q: text, limit: String(limit), extended: 'full' })}`;
  const res = await request(url, { token: null });
  if (res.status === 404) return [];
  if (!res.ok) throw new SimklError(`Simkl search failed (${res.status})`, res.status);
  const list = Array.isArray(res.data) ? res.data : [];
  return list.map((raw) => normalizeTitle(raw)).filter(Boolean);
}

async function getDetails(kind, id) {
  const path = kind === 'movie' ? 'movies' : 'tv';
  const res = await request(`${SIMKL_API}/${path}/${id}?${commonQuery({ extended: 'full' })}`);
  if (res.status === 404) return null;
  if (!res.ok || !res.data) throw new SimklError(`Simkl details failed (${res.status})`, res.status);
  return normalizeTitle(res.data, kind);
}

// Simkl stamps every kind of change. We compare these to skip unchanged users.
async function getActivities(token) {
  const res = await request(`${SIMKL_API}/sync/activities?${commonQuery()}`, { token });
  if (!res.ok) throw new SimklError(`Simkl activity failed (${res.status})`, res.status);
  return {
    movies: res.data?.movies?.all || null,
    shows: res.data?.tv_shows?.all || res.data?.tv?.all || null,
  };
}

// Everything a person has watched, as normalized entries with a watchedAt time.
async function getWatched(token, kind) {
  const bucket = kind === 'movie' ? 'movies' : 'shows';
  const res = await request(`${SIMKL_API}/sync/all-items/${bucket}?${commonQuery({ extended: 'full' })}`, { token });
  if (!res.ok) throw new SimklError(`Simkl library failed (${res.status})`, res.status);
  const list = Array.isArray(res.data) ? res.data : res.data?.[bucket] || [];
  const items = [];
  for (const entry of list) {
    const raw = entry.movie || entry.show || entry;
    const title = normalizeTitle(raw, kind);
    if (!title) continue;
    // last_watched is the episode code ("S02E05"); last_watched_at is the time.
    const watchedAt = entry.last_watched_at || null;
    const episodeCode = /^S\d+E\d+$/i.test(entry.last_watched || '') ? entry.last_watched.toUpperCase() : null;
    items.push({
      ...title,
      watchedAt,
      watchedEpisodes: entry.watched_episodes_count ?? null,
      totalEpisodes: entry.total_episodes_count ?? title.episodes,
      listStatus: entry.status || null, // watching, plantowatch, hold, completed, dropped
      addedAt: entry.added_to_watchlist_at || null,
      lastEpisode: episodeCode,
      userRating: typeof entry.user_rating === 'number' ? entry.user_rating : null,
    });
  }
  return items;
}

// Simkl's ready-made trending lists. kind: 'movie' or 'show'; period: today, week, month.
async function getTrending(kind, period = 'week', size = 100) {
  const folder = kind === 'movie' ? 'movies' : 'tv';
  const file = `${period}_${size >= 500 ? 500 : 100}.json`;
  const res = await request(`${SIMKL_DATA}/discover/trending/${folder}/${file}?${commonQuery()}`);
  if (!res.ok) throw new SimklError(`Simkl trending failed (${res.status})`, res.status);
  const list = Array.isArray(res.data) ? res.data : [];
  return list.map((raw) => normalizeTitle(raw, kind)).filter(Boolean);
}

// Pure: pick the watches newer than `sinceIso`, oldest first. The first run
// (no sinceIso) returns nothing, so an existing history is never dumped in chat.
function newWatches(items, sinceIso) {
  if (!sinceIso) return [];
  const since = Date.parse(sinceIso);
  return items
    .filter((i) => i.watchedAt && Date.parse(i.watchedAt) > since)
    .sort((a, b) => Date.parse(a.watchedAt) - Date.parse(b.watchedAt));
}

// Pure: the newest watch time across items, for the baseline on first track.
function latestWatch(items) {
  let best = null;
  for (const i of items) {
    if (i.watchedAt && (!best || Date.parse(i.watchedAt) > Date.parse(best))) best = i.watchedAt;
  }
  return best;
}

async function startDeviceLogin() {
  const res = await request(`${SIMKL_API}/oauth2/device?${commonQuery()}`, {
    method: 'POST',
    form: { client_id: clientId(), scope: DEVICE_SCOPE },
  });
  if (!res.ok || !res.data?.device_code) throw new SimklError(`Simkl sign-in couldn't start (${res.status})`, res.status);
  return {
    userCode: res.data.user_code,
    verificationUrl: res.data.verification_uri || 'https://simkl.com/device',
    deviceCode: res.data.device_code,
    interval: Math.max(5, Number(res.data.interval) || 5),
    expiresIn: Number(res.data.expires_in) || 900,
  };
}

// One check of a device code. Returns { state, token? } where state is
// 'pending', 'slow_down', 'done', 'expired', or 'denied'.
async function pollDeviceLogin(deviceCode) {
  const res = await request(`${SIMKL_API}/oauth2/token?${commonQuery()}`, {
    method: 'POST',
    form: {
      grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
      client_id: clientId(),
      device_code: deviceCode,
    },
  });
  if (res.ok && res.data?.access_token) return { state: 'done', ...tokenFields(res.data) };
  const error = res.data?.error;
  if (error === 'authorization_pending') return { state: 'pending' };
  if (error === 'slow_down') return { state: 'slow_down' };
  if (error === 'expired_token') return { state: 'expired' };
  if (error === 'access_denied') return { state: 'denied' };
  throw new SimklError(`Simkl sign-in failed (${res.status})`, res.status);
}

function tokenFields(data) {
  return {
    token: data.access_token,
    refreshToken: data.refresh_token || null,
    expiresIn: Number(data.expires_in) || null,
    scope: data.scope || null,
  };
}

// Swaps a renewal token for a fresh sign-in. Returns the same shape as a sign-in.
async function refreshLogin(refreshToken) {
  const res = await request(`${SIMKL_API}/oauth2/token?${commonQuery()}`, {
    method: 'POST',
    form: { grant_type: 'refresh_token', client_id: clientId(), refresh_token: refreshToken },
  });
  if (res.ok && res.data?.access_token) {
    const fields = tokenFields(res.data);
    return { ...fields, refreshToken: fields.refreshToken || refreshToken };
  }
  throw new SimklError(`Simkl couldn't renew the sign-in (${res.status} ${res.data?.error || ''})`.trim(), res.status);
}

// Who a token belongs to: { user: { name, avatar }, account: { id } }.
async function getAccount(token) {
  const res = await request(`${SIMKL_API}/users/settings?${commonQuery()}`, { token });
  if (!res.ok) throw new SimklError(`Simkl account lookup failed (${res.status})`, res.status);
  const user = res.data?.user || {};
  const account = res.data?.account || {};
  const name = user.name || 'Simkl user';
  return {
    id: String(account.id || name),
    name: String(name),
    avatar: user.avatar ? posterUrl(user.avatar) : null,
    siteUrl: 'https://simkl.com',
  };
}

module.exports = {
  SimklError,
  isConfigured,
  posterUrl,
  fanartUrl,
  normalizeTitle,
  searchTitles,
  getDetails,
  getActivities,
  getWatched,
  getTrending,
  newWatches,
  latestWatch,
  startDeviceLogin,
  pollDeviceLogin,
  refreshLogin,
  getAccount,
};
