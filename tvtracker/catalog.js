/* ---------------------------------------------------------------------------
 * Simkl, in the shape the bot's features expect.
 *
 * TV Tracker started as AniTracker, which reads AniList. Rather than rewrite
 * every feature, this module offers the same functions AniTracker's anilist.js
 * offered (getRecentFeed, getMediaById, getMediaDetails, …) and returns data
 * in AniList's shape, but every value comes from Simkl. Anime is left out.
 *
 *  - A person is identified by their Simkl account id (stored as anilistId on
 *    a linked account). Simkl only shares a watch history with that person's
 *    own sign-in, so only signed-in people can be tracked.
 *  - A media id is one number holding the Simkl id and the type:
 *      movie -> simklId * 2        show -> simklId * 2 + 1
 *  - media.type is 'ANIME' for everything (the features use it to mean
 *    "something you watch", as opposed to manga); media.format is 'MOVIE' or
 *    'TV' and media.kind is 'movie' or 'show'.
 * ------------------------------------------------------------------------- */
const simkl = require('./simkl');

const DEFAULT_RUNTIME = { movie: 110, show: 45 };
const RENEW_EARLY_MS = 24 * 60 * 60 * 1000;
const LIBRARY_RECHECK_MS = 60 * 1000;

let deps = { loadData: null, saveData: null };
function init(d) {
  deps = { ...deps, ...d };
}

/* ------------------------------ media shape ------------------------------- */

function encodeId(kind, simklId) {
  return Number(simklId) * 2 + (kind === 'show' ? 1 : 0);
}

function decodeId(mediaId) {
  const n = Number(mediaId);
  if (!Number.isFinite(n) || n <= 1) return null;
  return { kind: n % 2 ? 'show' : 'movie', id: Math.floor(n / 2) };
}

// "movie_123" / "show_9", the keys used before profiles existed.
function idFromKey(key) {
  const [kind, id] = String(key || '').split('_');
  if ((kind !== 'movie' && kind !== 'show') || !parseInt(id, 10)) return null;
  return encodeId(kind, parseInt(id, 10));
}

function mediaStatus(t) {
  const s = String(t.status || '').toLowerCase();
  if (/air|ongoing|returning|production/.test(s)) return 'RELEASING';
  if (/tba|planned|upcoming|announced/.test(s)) return 'NOT_YET_RELEASED';
  if (/cancel/.test(s)) return 'CANCELLED';
  return 'FINISHED';
}

// A Simkl title (from simkl.js) shaped like an AniList Media object.
function toMedia(t) {
  if (!t) return null;
  const year = t.year || null;
  return {
    id: encodeId(t.kind, t.id),
    kind: t.kind,
    simklId: t.id,
    type: 'ANIME',
    format: t.kind === 'movie' ? 'MOVIE' : 'TV',
    status: mediaStatus(t),
    episodes: t.kind === 'movie' ? 1 : t.episodes || t.totalEpisodes || null,
    chapters: null,
    duration: t.runtime || null,
    season: null,
    seasonYear: year,
    startDate: { year, month: null, day: null },
    averageScore: t.rating ? Math.round(t.rating * 10) : null,
    meanScore: t.rating ? Math.round(t.rating * 10) : null,
    genres: t.genres || [],
    tags: [],
    isAdult: false,
    title: { romaji: t.title, english: t.title, native: null },
    siteUrl: t.siteUrl,
    description: t.overview || null,
    coverImage: { extraLarge: t.poster, large: t.poster, medium: t.posterSmall || t.poster, color: null },
    bannerImage: t.fanart || null,
    nextAiringEpisode: null,
    trailers: t.trailers || [],
    network: t.network || null,
    imdb: t.imdb || null,
  };
}

function secondsOf(iso) {
  const t = Date.parse(iso || '');
  return Number.isFinite(t) ? Math.floor(t / 1000) : 0;
}

/* --------------------------- accounts and tokens -------------------------- */

// The linked account for a Simkl account id: { discordId, account, data }.
async function accountFor(simklId) {
  if (!deps.loadData) return null;
  const data = await deps.loadData();
  for (const [discordId, account] of Object.entries(data.linkedAccounts || {})) {
    if (String(account.anilistId) === String(simklId) && account.token) return { discordId, account, data };
  }
  return null;
}

// A working token for someone. Simkl sign-ins last about 7 days, so they're
// renewed a day early, or right away (force) after Simkl rejects one.
async function tokenFor(simklId, { force = false } = {}) {
  const found = await accountFor(simklId);
  if (!found) return null;
  const { account, data } = found;
  const dueSoon = account.expiresAt && Date.now() > account.expiresAt - RENEW_EARLY_MS;
  if (!account.refreshToken || (!force && !dueSoon)) return account.token;
  try {
    const renewed = await simkl.refreshLogin(account.refreshToken);
    account.token = renewed.token;
    account.refreshToken = renewed.refreshToken;
    account.expiresAt = renewed.expiresIn ? Date.now() + renewed.expiresIn * 1000 : null;
    if (deps.saveData) await deps.saveData(data);
    console.log(`Renewed ${account.anilistName}'s Simkl sign-in.`);
  } catch (e) {
    console.warn(`Couldn't renew ${account.anilistName}'s Simkl sign-in: ${e.message}`);
  }
  return account.token;
}

class NotSignedIn extends Error {
  constructor() {
    super('Not signed in to Simkl (they need to run /signin)');
    this.status = 401;
  }
}

/* -------------------------------- libraries ------------------------------- */

// Everything someone has on Simkl (movies + shows). Simkl's activity stamps
// are checked first, so the full library is only downloaded after a change.
const libraries = new Map(); // simklId -> { stamps, items, at }

async function libraryOf(simklId, { fresh = false } = {}) {
  const key = String(simklId);
  const held = libraries.get(key);
  if (held && !fresh && Date.now() - held.at < LIBRARY_RECHECK_MS) return held.items;
  let token = await tokenFor(simklId);
  if (!token) throw new NotSignedIn();
  let stamps;
  try {
    stamps = await simkl.getActivities(token);
  } catch (e) {
    if (e.status !== 401) throw e;
    token = await tokenFor(simklId, { force: true });
    stamps = await simkl.getActivities(token);
  }
  if (held && held.stamps.movies === stamps.movies && held.stamps.shows === stamps.shows) {
    held.at = Date.now();
    return held.items;
  }
  const [movies, shows] = await Promise.all([simkl.getWatched(token, 'movie'), simkl.getWatched(token, 'show')]);
  const items = [...movies, ...shows];
  libraries.set(key, { stamps, items, at: Date.now() });
  if (libraries.size > 500) libraries.delete(libraries.keys().next().value);
  return items;
}

const LIST_STATUS = { watching: 'CURRENT', completed: 'COMPLETED', plantowatch: 'PLANNING', dropped: 'DROPPED', hold: 'PAUSED' };

// One library item as an AniList list entry: { status, progress, updatedAt, score, media }.
function entryOf(item) {
  let status = LIST_STATUS[item.listStatus] || null;
  if (!status && item.kind === 'movie' && item.watchedAt) status = 'COMPLETED';
  if (!status) return null;
  const progress = item.kind === 'show' ? item.watchedEpisodes || 0 : status === 'COMPLETED' ? 1 : 0;
  return {
    id: encodeId(item.kind, item.id),
    status,
    progress,
    repeat: 0,
    updatedAt: Math.max(secondsOf(item.watchedAt), secondsOf(item.addedAt)),
    score: item.userRating || 0,
    lastEpisode: item.lastEpisode || null,
    media: toMedia(item),
  };
}

/* ------------------- the functions the features call --------------------- */

// /track by name: only people who signed in with /signin can be found.
async function getUserByName(name) {
  if (!deps.loadData) return null;
  const want = String(name || '').trim().toLowerCase();
  const data = await deps.loadData();
  for (const account of Object.values(data.linkedAccounts || {})) {
    if (account.token && String(account.anilistName || '').toLowerCase() === want) {
      return { id: account.anilistId, name: account.anilistName, siteUrl: account.siteUrl, avatar: { medium: account.avatar, large: account.avatar } };
    }
  }
  return null;
}

// The newest watches, shaped like AniList list activities.
async function getRecentActivities(simklId, perPage = 10) {
  const items = await libraryOf(simklId);
  return items
    .filter((i) => i.watchedAt)
    .sort((a, b) => Date.parse(b.watchedAt) - Date.parse(a.watchedAt))
    .slice(0, perPage)
    .map((i) => ({
      __typename: 'ListActivity',
      id: secondsOf(i.watchedAt),
      status: i.kind === 'movie' ? 'completed' : 'watched episode',
      progress: i.kind === 'show' && i.watchedEpisodes ? String(i.watchedEpisodes) : null,
      createdAt: secondsOf(i.watchedAt),
      media: toMedia(i),
    }));
}

// One request per poll: no activity stream (Simkl has none), just the most
// recently updated list entries. features/feed.js turns their changes into
// posts, the same way it handles AniList list updates without an activity.
async function getRecentFeed(simklId) {
  // The poller always checks Simkl's change stamps (cheap); the full library
  // is only downloaded when they moved.
  const items = await libraryOf(simklId, { fresh: true });
  const entries = items
    .map(entryOf)
    .filter(Boolean)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, 12);
  return { activities: [], entries };
}

// Simkl has genres but not AniList-style tags, so a title's genres are used
// as its tags (posts show them; category muting reads them). Cached lookups.
async function getMediaTags(ids) {
  const out = new Map();
  await Promise.all(
    ids.map(async (id) => {
      const media = await getMediaById(id).catch(() => null);
      out.set(Number(id), (media?.genres || []).map((name) => ({ name, rank: 100, category: 'Genre', isMediaSpoiler: false })));
    })
  );
  return out;
}

// Several people's entries for one title (/watched).
async function getEntriesForMedia(mediaId, userIds) {
  const want = Number(mediaId);
  const out = [];
  for (const simklId of [...new Set(userIds.map(Number).filter((n) => n > 0))].slice(0, 50)) {
    try {
      const items = await libraryOf(simklId);
      const item = items.find((i) => encodeId(i.kind, i.id) === want);
      const entry = item && entryOf(item);
      if (entry) out.push({ userId: simklId, status: entry.status, progress: entry.progress, repeat: 0, updatedAt: entry.updatedAt, score: entry.score });
    } catch {
      /* not signed in any more: skip them */
    }
  }
  return out;
}

const detailCache = new Map(); // mediaId -> { at, media }
async function getMediaById(mediaId) {
  const ref = decodeId(mediaId);
  if (!ref) return null;
  const hit = detailCache.get(Number(mediaId));
  if (hit && Date.now() - hit.at < 30 * 60 * 1000) return hit.media;
  const media = toMedia(await simkl.getDetails(ref.kind, ref.id));
  if (media) {
    detailCache.set(Number(mediaId), { at: Date.now(), media });
    if (detailCache.size > 800) detailCache.delete(detailCache.keys().next().value);
  }
  return media;
}

// Simkl has no AniList-style favourites list.
async function getUserFavorites() {
  return null;
}

// No season chains on Simkl: every title is its own root.
async function getMediaRelations(mediaId) {
  const media = await getMediaById(mediaId);
  if (!media) return null;
  return { id: media.id, type: media.type, format: media.format, rankings: [], relations: { edges: [] } };
}

// Everything the More Info panel shows.
async function getMediaDetails(mediaId) {
  const media = await getMediaById(mediaId);
  if (!media) return null;
  const trailer = media.trailers[0];
  return {
    ...media,
    idMal: null,
    endDate: { year: null, month: null, day: null },
    source: null,
    countryOfOrigin: null,
    popularity: null,
    favourites: null,
    volumes: null,
    studios: { nodes: media.network ? [{ name: media.network, siteUrl: null }] : [] },
    trailer: trailer ? { id: trailer.youtube, site: 'youtube' } : null,
    externalLinks: [
      ...(media.imdb ? [{ site: 'IMDb', url: `https://www.imdb.com/title/${media.imdb}`, type: 'INFO', isDisabled: false }] : []),
      { site: 'Simkl', url: media.siteUrl, type: 'INFO', isDisabled: false },
    ],
    rankings: [],
    characters: { edges: [] },
  };
}

// Lifetime totals, what they're watching and their top-rated title.
async function getProfileSnapshot(simklId) {
  const found = await accountFor(simklId);
  if (!found) return null;
  const items = await libraryOf(simklId);
  const statuses = {};
  const genres = new Map();
  let episodes = 0;
  let minutes = 0;
  let scoreSum = 0;
  let scored = 0;
  let top = null;
  for (const item of items) {
    statuses[item.listStatus || 'none'] = (statuses[item.listStatus || 'none'] || 0) + 1;
    const units = item.kind === 'movie' ? (item.listStatus === 'completed' || item.watchedAt ? 1 : 0) : item.watchedEpisodes || 0;
    const mins = units * (item.runtime || DEFAULT_RUNTIME[item.kind]);
    episodes += units;
    minutes += mins;
    if (units) {
      for (const g of item.genres || []) {
        const e = genres.get(g) || { genre: g, count: 0, minutes: 0 };
        e.count += 1;
        e.minutes += mins;
        genres.set(g, e);
      }
    }
    if (item.userRating) {
      scoreSum += item.userRating;
      scored += 1;
      if (!top || item.userRating > top.score) top = { score: item.userRating, media: toMedia(item) };
    }
  }
  const current = items
    .filter((i) => i.kind === 'show' && i.listStatus === 'watching')
    .map(entryOf)
    .filter(Boolean)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, 15);
  const a = found.account;
  const genreList = [...genres.values()].sort((x, y) => y.count - x.count).slice(0, 6);
  return {
    user: { id: a.anilistId, name: a.anilistName, siteUrl: a.siteUrl || 'https://simkl.com', avatar: { large: a.avatar, medium: a.avatar }, bannerImage: null },
    lifetime: {
      entries: items.length,
      meanScore: scored ? Math.round((scoreSum / scored) * 10) : 0,
      minutes,
      episodes,
      completed: statuses.completed || 0,
      watching: statuses.watching || 0,
      planning: statuses.plantowatch || 0,
      dropped: statuses.dropped || 0,
      paused: statuses.hold || 0,
      genres: genreList,
      tags: genreList,
      topRated: top,
    },
    current,
  };
}

function fuzzyInt(date) {
  return date.getUTCFullYear() * 10000 + (date.getUTCMonth() + 1) * 100 + date.getUTCDate();
}

// Titles finished between two dates (YYYYMMDD numbers), highest rated first.
async function getCompletedBetween(simklId, fromInt, toInt) {
  const items = await libraryOf(simklId);
  return items
    .filter((i) => (i.listStatus === 'completed' || (i.kind === 'movie' && !i.listStatus)) && i.watchedAt)
    .map((i) => ({ i, d: new Date(i.watchedAt) }))
    .filter(({ d }) => fuzzyInt(d) > fromInt && fuzzyInt(d) < toInt)
    .sort((x, y) => (y.i.userRating || 0) - (x.i.userRating || 0))
    .map(({ i, d }) => ({
      score: i.userRating || 0,
      completedAt: { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() },
      media: toMedia(i),
    }));
}

const trendingCache = new Map();
async function getTrendingMedia(perPage = 10, sort = 'TRENDING_DESC') {
  const period = sort === 'TRENDING_DESC' ? 'week' : 'month';
  const key = `${sort}|${perPage}`;
  const hit = trendingCache.get(key);
  if (hit && Date.now() - hit.at < 30 * 60 * 1000) return hit.list;
  if (sort === 'SCORE_DESC') {
    // Highest rated among this month's 500 most-watched movies and shows.
    const [m500, s500] = await Promise.all([simkl.getTrending('movie', 'month', 500), simkl.getTrending('show', 'month', 500)]);
    const list = [...m500, ...s500]
      .filter((t) => t.rating)
      .sort((x, y) => y.rating - x.rating)
      .slice(0, perPage)
      .map(toMedia);
    trendingCache.set(key, { at: Date.now(), list });
    return list;
  }
  const [movies, shows] = await Promise.all([simkl.getTrending('movie', period, 100), simkl.getTrending('show', period, 100)]);
  // Alternate movies and shows so both appear near the top.
  const list = [];
  for (let k = 0; list.length < perPage && (k < movies.length || k < shows.length); k++) {
    if (shows[k]) list.push(toMedia(shows[k]));
    if (movies[k] && list.length < perPage) list.push(toMedia(movies[k]));
  }
  trendingCache.set(key, { at: Date.now(), list });
  return list;
}

// A random title from this month's 500 most-watched movies and shows,
// optionally of one genre (checked against a few candidates' details).
async function getRandomMedia(genre = null) {
  const [movies, shows] = await Promise.all([simkl.getTrending('movie', 'month', 500), simkl.getTrending('show', 'month', 500)]);
  const pool = [...movies, ...shows];
  if (!pool.length) return null;
  const pick = () => pool[Math.floor(Math.random() * pool.length)];
  if (!genre) {
    const t = pick();
    return encodeId(t.kind, t.id);
  }
  const want = genre.toLowerCase();
  for (let tries = 0; tries < 8; tries++) {
    const t = pick();
    const media = await getMediaById(encodeId(t.kind, t.id)).catch(() => null);
    if (media && media.genres.some((g) => g.toLowerCase() === want)) return media.id;
  }
  return null;
}

// Trailers stand in for anime openings ("favorite trailer" on profiles).
async function getTrailers(mediaId) {
  const media = await getMediaById(mediaId);
  return (media?.trailers || []).slice(0, 10).map((t, i) => ({
    slug: `Trailer ${i + 1}`,
    song: t.name || `Trailer ${i + 1}`,
    artists: [],
    episodes: null,
    videoUrl: `https://www.youtube.com/watch?v=${encodeURIComponent(t.youtube)}`,
    pageUrl: media.siteUrl,
  }));
}

async function searchMedia(query, perPage = 10) {
  return (await simkl.searchTitles(query, 'all', Math.min(perPage, 20))).map(toMedia);
}

module.exports = {
  init,
  encodeId,
  decodeId,
  idFromKey,
  toMedia,
  accountFor,
  tokenFor,
  libraryOf,
  entryOf,
  getUserByName,
  getRecentActivities,
  getRecentFeed,
  getMediaTags,
  getEntriesForMedia,
  getMediaById,
  getUserFavorites,
  getMediaRelations,
  getMediaDetails,
  getProfileSnapshot,
  getCompletedBetween,
  getTrendingMedia,
  getRandomMedia,
  getTrailers,
  searchMedia,
  fuzzyInt,
};
