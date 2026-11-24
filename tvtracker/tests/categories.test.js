const test = require('node:test');
const assert = require('node:assert/strict');
const categories = require('../features/categories');

const anime = { genres: ['Anime', 'Action'] };
const doc = { genres: [], tags: [{ name: 'Documentary' }] };
const drama = { genres: ['Drama'] };

test('titles fall into categories by their genres or genre tags', () => {
  assert.deepEqual(categories.categoriesOf(anime), ['anime']);
  assert.deepEqual(categories.categoriesOf(doc), ['documentary']);
  assert.deepEqual(categories.categoriesOf(drama), []);
  assert.deepEqual(categories.categoriesOf({ genres: ['Animation', 'Family'] }), ['animation', 'kids']);
});

test('a muted category drops a title, others pass', () => {
  assert.equal(categories.mutedBy(anime, ['anime']), 'anime');
  assert.equal(categories.mutedBy(drama, ['anime', 'horror']), null);
  assert.equal(categories.mutedBy(doc, []), null);
  assert.equal(categories.mutedBy(null, ['anime']), null);
  const updates = [{ media: anime }, { media: drama }, { media: doc }];
  assert.deepEqual(updates.filter((u) => !categories.mutedBy(u.media, ['anime', 'documentary'])).map((u) => u.media), [drama]);
});
