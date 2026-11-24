// AniTracker c029d74 (profile glow-up) as adapted for TV Tracker:
// favorite trailers (up to 3, no endings), 18+ covers as spoilers, the
// status bubble + @handle on watch posts.
const test = require('node:test');
const assert = require('node:assert/strict');
const social = require('../features/social');
const media = require('../features/media');
const activityPost = require('../features/activityPost');
const guildConfig = require('../features/guildConfig');

test('favorite trailers: up to 3, older single trailer still read', () => {
  const p = social.defaults().profile;
  assert.deepEqual(social.songsOf(p, 'OP'), []);
  const old = { opening: { slug: 'Trailer 1', song: 'Official Trailer', mediaId: 2 } };
  assert.equal(social.songsOf(old, 'OP').length, 1);
  social.setSongs(p, 'OP', [1, 2, 3, 4].map((i) => ({ slug: `Trailer ${i}`, song: `T${i}`, mediaId: i })));
  assert.equal(p.openings.length, social.MAX_SONGS);
  assert.equal(p.opening.song, 'T1');
});

test('no endings section on TV Tracker profiles', () => {
  assert.equal(social.SECTIONS.ending, undefined);
  assert.ok(!social.DEFAULT_SECTIONS.includes('ending'));
  for (const l of Object.values(social.LAYOUTS)) assert.ok(!l.order.includes('ending'));
  assert.match(social.SECTIONS.opening.label, /trailer/i);
});

test('endings lookup answers empty (Simkl has none)', async () => {
  assert.deepEqual(await media.getOpenings(123, 'ED'), []);
});

test('18+ cover defaults to a spoiler attachment, "hide" drops it', () => {
  assert.equal(guildConfig.DEFAULTS.mature, 'blur');
  const activity = {
    __typename: 'ListActivity',
    status: 'watched episode',
    progress: '3',
    createdAt: 1700000000,
    media: { id: 3, type: 'ANIME', format: 'TV', isAdult: true, title: { english: 'Show' }, siteUrl: 'https://simkl.com/tv/1', coverImage: { large: 'https://img.example/c.jpg' }, genres: [] },
  };
  const base = { username: 'sep', activity, streak: 2, look: { profile: { tagline: 'losing to my backlog' } }, discordName: 'sep_dc' };
  const blur = activityPost.buildActivityPost({ ...base, cfg: { mature: 'blur' } });
  assert.equal(blur.spoilerUrl, 'https://img.example/c.jpg');
  const e = blur.embeds[0].toJSON();
  assert.ok(!e.image && !e.thumbnail);
  assert.match(e.author.name, /sep 🔥 2 \| @sep_dc/);
  assert.match(e.author.url, /simkl\.com/);
  assert.match(e.description, /💬 losing to my backlog/);
  assert.match(e.description, /🕒/); // TV Tracker's watch time line stays
  const hide = activityPost.buildActivityPost({ ...base, cfg: { mature: 'hide' } });
  assert.equal(hide.spoilerUrl, null);
});

test('catch-up post links to Simkl', () => {
  const p = activityPost.buildCatchUpPost({ username: 'sep', items: [{ status: 'completed', title: 'X', text: 'done' }] });
  assert.match(p.embeds[0].toJSON().author.url, /simkl\.com/);
});

test('trailer card paints a PNG even without a cover', async (t) => {
  const art = require('../features/canvasArt');
  if (!art.available()) return t.skip('canvas not installed');
  const png = await art.paintSongs('OP', [{ slug: 'Trailer 1', song: 'Official Trailer', artist: '', anime: 'Some Show', cover: [] }]);
  assert.ok(Buffer.isBuffer(png) && png.length > 1000);
  assert.equal(png.subarray(1, 4).toString(), 'PNG');
});
