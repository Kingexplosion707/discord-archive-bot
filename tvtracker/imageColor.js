const Jimp = require('jimp');

const DEFAULT_COLOR = 0x3db4f2;

// Small LRU-ish cache so the same cover is never downloaded + decoded twice.
const colorCache = new Map();
const MAX_CACHE = 300;

/**
 * Downloads a small version of the image and picks the most common
 * non-extreme color as a stand-in for the "primary" color.
 * Falls back to DEFAULT_COLOR on any failure or timeout, so a bad/slow
 * image never blocks or crashes the poll loop.
 */
async function getDominantColor(imageUrl) {
  if (!imageUrl) return DEFAULT_COLOR;
  if (colorCache.has(imageUrl)) return colorCache.get(imageUrl);
  const color = await computeDominantColor(imageUrl);
  colorCache.set(imageUrl, color);
  if (colorCache.size > MAX_CACHE) colorCache.delete(colorCache.keys().next().value);
  return color;
}

// Callers should pass the SMALLEST cover size AniList offers (coverImage.medium):
// the colour comes out the same, but decoding a full-size "extraLarge" cover
// with Jimp briefly costs tens of MB of RAM — on Render's 512 MB free tier
// that was a real out-of-memory (→ restart) risk.
async function computeDominantColor(imageUrl) {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    const res = await fetch(imageUrl, { signal: controller.signal });
    clearTimeout(timeout);
    if (!res.ok) return DEFAULT_COLOR;

    const buffer = Buffer.from(await res.arrayBuffer());
    const image = await Jimp.read(buffer);
    image.resize(24, Jimp.AUTO); // shrink first, way faster to scan

    const colorCounts = {};
    image.scan(0, 0, image.bitmap.width, image.bitmap.height, function (x, y, idx) {
      const r = this.bitmap.data[idx];
      const g = this.bitmap.data[idx + 1];
      const b = this.bitmap.data[idx + 2];
      const a = this.bitmap.data[idx + 3];
      if (a < 128) return; // skip transparent pixels

      const brightness = (r + g + b) / 3;
      if (brightness > 240 || brightness < 15) return; // skip near-white/near-black

      // quantize so near-identical shades get grouped together
      const key = `${Math.round(r / 16) * 16},${Math.round(g / 16) * 16},${Math.round(b / 16) * 16}`;
      colorCounts[key] = (colorCounts[key] || 0) + 1;
    });

    let bestKey = null;
    let bestCount = 0;
    for (const [key, count] of Object.entries(colorCounts)) {
      if (count > bestCount) {
        bestCount = count;
        bestKey = key;
      }
    }

    if (!bestKey) return DEFAULT_COLOR;
    const [r, g, b] = bestKey.split(',').map(Number);
    return (r << 16) + (g << 8) + b;
  } catch (e) {
    return DEFAULT_COLOR;
  }
}

module.exports = { getDominantColor, DEFAULT_COLOR };
