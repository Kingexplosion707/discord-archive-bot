/* ---------------------------------------------------------------------------
 * Categories people can mute: "stop posting anything anime-related", or
 * "don't track documentaries at all". A category is a set of genre names;
 * a title is in it when any of its genres (or genre tags) match.
 * ------------------------------------------------------------------------- */

const CATEGORIES = [
  { key: 'anime', label: 'Anime', emoji: '🌸', genres: ['anime'] },
  { key: 'animation', label: 'Animation & cartoons', emoji: '🎨', genres: ['animation', 'cartoon'] },
  { key: 'kids', label: 'Kids & family', emoji: '🧸', genres: ['children', 'kids', 'family'] },
  { key: 'reality', label: 'Reality & game shows', emoji: '🎤', genres: ['reality', 'reality-tv', 'game show', 'game-show'] },
  { key: 'talk', label: 'Talk shows & news', emoji: '🎙️', genres: ['talk show', 'talk-show', 'news'] },
  { key: 'documentary', label: 'Documentary', emoji: '🌍', genres: ['documentary'] },
  { key: 'horror', label: 'Horror', emoji: '🩸', genres: ['horror'] },
  { key: 'romance', label: 'Romance', emoji: '💕', genres: ['romance'] },
  { key: 'sports', label: 'Sports', emoji: '🏟️', genres: ['sport', 'sports'] },
  { key: 'music', label: 'Music', emoji: '🎵', genres: ['music', 'musical'] },
];
const BY_KEY = new Map(CATEGORIES.map((c) => [c.key, c]));

function genreNames(media) {
  const names = [...(media?.genres || []), ...(media?.tags || []).map((t) => t?.name)];
  return names.filter(Boolean).map((g) => String(g).toLowerCase().trim());
}

// The category keys a movie or show belongs to.
function categoriesOf(media) {
  const genres = genreNames(media);
  return CATEGORIES.filter((c) => genres.some((g) => c.genres.includes(g))).map((c) => c.key);
}

// The first muted category this title falls in, or null.
function mutedBy(media, mutedKeys) {
  if (!media || !mutedKeys?.length) return null;
  const muted = new Set(mutedKeys);
  return categoriesOf(media).find((k) => muted.has(k)) || null;
}

function label(key) {
  const c = BY_KEY.get(key);
  return c ? `${c.emoji} ${c.label}` : key;
}

module.exports = { CATEGORIES, categoriesOf, mutedBy, label, isCategory: (k) => BY_KEY.has(k) };
