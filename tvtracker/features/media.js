const catalog = require('../catalog');

// Movie and TV lookups used by profiles, collections, reviews and stats.
// Same functions as AniTracker's anilistSocial.js; the data comes from Simkl
// (see catalog.js). Searches are cached briefly so browsing stays quick.

const searchCache = new Map(); // "query|n" -> { at, list }

function titleOf(m) {
  return m?.title?.english || m?.title?.romaji || 'Unknown title';
}

// Compact copy stored inside collections and reviews, so lists render without
// a lookup per item. Simkl stays the source of truth.
function snapshot(m) {
  return {
    mediaId: m.id,
    title: titleOf(m),
    cover: m.coverImage?.extraLarge || m.coverImage?.large || m.coverImage?.medium || null,
    banner: m.bannerImage || null,
    siteUrl: m.siteUrl || 'https://simkl.com',
    color: m.coverImage?.color || null,
    year: m.seasonYear || null,
    format: m.format || null,
    episodes: m.episodes || null,
  };
}

async function searchAnime(query, perPage = 10) {
  const q = (query || '').trim();
  if (q.length < 2) return [];
  const key = `${q.toLowerCase()}|${perPage}`;
  const hit = searchCache.get(key);
  if (hit && Date.now() - hit.at < 10 * 60 * 1000) return hit.list;
  const list = await catalog.searchMedia(q, perPage);
  searchCache.set(key, { at: Date.now(), list });
  if (searchCache.size > 500) searchCache.delete(searchCache.keys().next().value);
  return list;
}

function getMediaLite(id) {
  return catalog.getMediaById(id);
}

// Several titles at once (art cards). Simkl has no batch lookup, so this asks
// for each one (they're cached after the first time).
async function getMediaBatch(ids) {
  const want = [...new Set(ids.map(Number).filter((n) => n > 1))].slice(0, 50);
  const out = new Map();
  await Promise.all(
    want.map(async (id) => {
      const m = await catalog.getMediaById(id).catch(() => null);
      if (m) out.set(id, m);
    })
  );
  return out;
}

function getProfileSnapshot(simklId) {
  return catalog.getProfileSnapshot(simklId);
}

function getCompletedBetween(simklId, fromInt, toInt) {
  return catalog.getCompletedBetween(simklId, fromInt, toInt);
}

function getTrending(perPage = 10, sort = 'TRENDING_DESC') {
  return catalog.getTrendingMedia(perPage, sort);
}

// (genre, retry, vibe): Simkl has no tags, so a "vibe" is treated as a genre.
function getRandomAnime(genre = null, retry = false, tag = null) {
  return catalog.getRandomMedia(genre || tag);
}

// Movies and shows have no anime openings: a profile's favorite "opening" is
// a favorite trailer, from the title's Simkl trailers.
// Trailers stand in for openings. Simkl has nothing like endings, so kind
// 'ED' always answers an empty list.
async function getOpenings(mediaId, kind = 'OP') {
  if (kind === 'ED') return [];
  return catalog.getTrailers(mediaId);
}

module.exports = {
  titleOf,
  snapshot,
  searchAnime,
  getMediaLite,
  getMediaBatch,
  getProfileSnapshot,
  getCompletedBetween,
  getOpenings,
  getTrending,
  getRandomAnime,
  fuzzyInt: catalog.fuzzyInt,
};
