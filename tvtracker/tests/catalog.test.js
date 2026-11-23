const test = require('node:test');
const assert = require('node:assert/strict');

process.env.SIMKL_CLIENT_ID = 'test-client';
const simkl = require('../simkl');
const catalog = require('../catalog');
const feed = require('../features/feed');
const statsEngine = require('../features/statsEngine');
const activityPost = require('../features/activityPost');
const { buildMoreInfo } = require('../moreInfo');

const T = (iso) => new Date(iso).toISOString();
const SHOW = { kind: 'show', id: 9, title: 'Severance', year: 2022, poster: 'https://img/p.jpg', posterSmall: 'https://img/s.jpg', fanart: 'https://img/f.jpg', siteUrl: 'https://simkl.com/tv/9/severance', genres: ['Drama', 'Thriller'], runtime: 50, episodes: 19, trailers: [{ name: 'Season 2 Trailer', youtube: 'abc123' }], rating: 8.7, overview: 'Work and life, split.' };
const MOVIE = { kind: 'movie', id: 77, title: 'Dune', year: 2021, poster: 'https://img/d.jpg', siteUrl: 'https://simkl.com/movies/77/dune', genres: ['Science Fiction'], runtime: 155, imdb: 'tt1160419', trailers: [], rating: 8.0 };

let library = [];
let stamps = { movies: 'a', shows: 'a' };
let libraryDownloads = 0;
simkl.getActivities = async () => stamps;
simkl.getWatched = async (_token, kind) => {
  libraryDownloads += 1;
  return library.filter((i) => i.kind === kind);
};
simkl.getDetails = async (kind, id) => (kind === 'movie' && id === 77 ? MOVIE : kind === 'show' && id === 9 ? SHOW : null);

const data = { linkedAccounts: { 111: { anilistId: 500, anilistName: 'king', avatar: null, siteUrl: 'https://simkl.com', token: 'tok' } } };
catalog.init({ loadData: async () => data, saveData: async () => {} });

const showId = catalog.encodeId('show', 9);
const movieId = catalog.encodeId('movie', 77);

test('media ids keep the type and the Simkl id', () => {
  assert.equal(showId, 19);
  assert.equal(movieId, 154);
  assert.deepEqual(catalog.decodeId(showId), { kind: 'show', id: 9 });
  assert.deepEqual(catalog.decodeId(movieId), { kind: 'movie', id: 77 });
  assert.equal(catalog.idFromKey('movie_77'), movieId);
  assert.equal(catalog.decodeId(1), null);
});

test('Simkl watches become posts, stats and info cards through the upstream code', async () => {
  // First poll: they're watching Severance (3 eps) and plan to watch Dune.
  library = [
    { ...SHOW, listStatus: 'watching', watchedEpisodes: 3, totalEpisodes: 19, watchedAt: T('2026-11-20T20:00:00Z') },
    { ...MOVIE, listStatus: 'plantowatch', watchedAt: null, addedAt: T('2026-11-19T10:00:00Z') },
  ];
  const state = {};
  let f = await catalog.getRecentFeed(500);
  assert.deepEqual(f.activities, []);
  assert.equal(f.entries.length, 2);
  let ev = feed.expand(state, f.activities, f.entries);
  assert.deepEqual([ev.merged.length, ev.synthetic.length], [0, 0], 'first poll only remembers, no backlog');

  // Nothing changed on Simkl: the library isn't downloaded again.
  const before = libraryDownloads;
  f = await catalog.getRecentFeed(500);
  assert.equal(libraryDownloads, before);
  ev = feed.expand(state, f.activities, f.entries);
  assert.equal(ev.synthetic.length, 0);

  // They watch two more episodes, then finish Dune.
  stamps = { movies: 'b', shows: 'b' };
  library = [
    { ...SHOW, listStatus: 'watching', watchedEpisodes: 5, totalEpisodes: 19, watchedAt: T('2026-11-21T21:00:00Z'), lastEpisode: 'S01E05' },
    { ...MOVIE, listStatus: 'completed', watchedAt: T('2026-11-21T23:00:00Z'), addedAt: T('2026-11-19T10:00:00Z'), userRating: 9 },
  ];
  f = await catalog.getRecentFeed(500);
  ev = feed.expand(state, f.activities, f.entries);
  const byTitle = Object.fromEntries(ev.synthetic.map((e) => [e.media.title.english, e]));
  assert.equal(ev.synthetic.length, 2);
  assert.equal(byTitle.Severance.status, 'watched episode');
  assert.equal(byTitle.Severance.progress, '4 - 5');
  assert.equal(byTitle.Dune.status, 'completed');

  // Stats: 2 episodes x 50 min, plus one 155 min movie.
  const stats = { users: {}, media: {} };
  statsEngine.record(stats, 500, [], 'UTC', ev.synthetic);
  const month = stats.users['500'].months['2026-11'];
  assert.equal(month.e, 3);
  assert.equal(month.m, 2 * 50 + 155);
  assert.equal(month.c, 1);

  // Each event renders as a post that names the title.
  for (const e of ev.synthetic) {
    const post = activityPost.buildActivityPost({ username: 'king', activity: e, streak: 2, look: null, ownerId: null, mentionId: null, color: 0x3db4f2, cfg: {} });
    const json = JSON.stringify(post);
    assert.match(json, new RegExp(e.media.title.english));
    assert.doesNotMatch(json, /anilist\.co/);
  }

  // More Info works with what Simkl has.
  const details = await catalog.getMediaDetails(movieId);
  const info = JSON.stringify(buildMoreInfo(details));
  assert.match(info, /Dune/);
  assert.match(info, /imdb\.com\/title\/tt1160419/);
});

test('profiles, /watched and recaps read the same library', async () => {
  const snap = await catalog.getProfileSnapshot(500);
  assert.equal(snap.user.name, 'king');
  assert.equal(snap.lifetime.episodes, 6); // 5 episodes + 1 movie
  assert.equal(snap.lifetime.completed, 1);
  assert.equal(snap.lifetime.topRated.media.title.english, 'Dune');
  assert.equal(snap.current[0].media.title.english, 'Severance');
  assert.equal(snap.current[0].progress, 5);

  const entries = await catalog.getEntriesForMedia(showId, [500, 999]);
  assert.deepEqual(entries.map((e) => [e.userId, e.status, e.progress]), [[500, 'CURRENT', 5]]);

  const done = await catalog.getCompletedBetween(500, 20261100, 20261201);
  assert.deepEqual(done.map((d) => [d.media.title.english, d.score]), [['Dune', 9]]);

  const trailers = await catalog.getTrailers(showId);
  assert.equal(trailers[0].videoUrl, 'https://www.youtube.com/watch?v=abc123');

  assert.equal(await catalog.getUserByName('KING').then((u) => u.id), 500);
  assert.equal(await catalog.getUserByName('nobody'), null);
});

test('someone who signed out has no feed', async () => {
  await assert.rejects(catalog.getRecentFeed(4242), (e) => e.status === 401);
});
