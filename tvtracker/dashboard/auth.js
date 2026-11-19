const crypto = require('crypto');

const COOKIE_NAME = 'atk_session';
const SESSION_MS = 12 * 60 * 60 * 1000; // 12 hours

// Dashboard has no user accounts — it's one shared password (DASHBOARD_PASSWORD)
// protecting actions like sending messages, pausing the bot, and redeploying.
// Sessions are a signed, httpOnly cookie: {expiry}.{hmac(expiry)} — no server-side
// session store needed, which keeps this working across Render restarts without
// another moving part. The signature just proves "this cookie was issued by us
// and hasn't been tampered with"; nothing sensitive is stored inside it.
function getSecret() {
  const secret = process.env.DASHBOARD_SESSION_SECRET;
  if (!secret) {
    // Falls back to a random secret so the app still boots, but this means
    // every restart invalidates existing sessions. Set DASHBOARD_SESSION_SECRET
    // in your env for real use.
    if (!getSecret._fallback) {
      console.warn(
        'DASHBOARD_SESSION_SECRET is not set — using a random per-process secret. Dashboard logins will not survive a restart until you set this.'
      );
      getSecret._fallback = crypto.randomBytes(32).toString('hex');
    }
    return getSecret._fallback;
  }
  return secret;
}

function sign(value) {
  return crypto.createHmac('sha256', getSecret()).update(value).digest('hex');
}

function createSessionCookieValue() {
  const expiry = String(Date.now() + SESSION_MS);
  return `${expiry}.${sign(expiry)}`;
}

function isValidSessionCookieValue(value) {
  if (!value || typeof value !== 'string') return false;
  const [expiry, signature] = value.split('.');
  if (!expiry || !signature) return false;
  const expected = sign(expiry);
  // Constant-time compare to avoid leaking the signature via timing.
  const a = Buffer.from(signature);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return false;
  return Number(expiry) > Date.now();
}

function parseCookies(req) {
  const header = req.headers.cookie;
  const out = {};
  if (!header) return out;
  for (const part of header.split(';')) {
    const idx = part.indexOf('=');
    if (idx === -1) continue;
    const key = part.slice(0, idx).trim();
    const val = part.slice(idx + 1).trim();
    out[key] = decodeURIComponent(val);
  }
  return out;
}

function setSessionCookie(res) {
  const value = createSessionCookieValue();
  const secure = process.env.NODE_ENV === 'production' ? '; Secure' : '';
  res.setHeader(
    'Set-Cookie',
    `${COOKIE_NAME}=${encodeURIComponent(value)}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${Math.floor(
      SESSION_MS / 1000
    )}${secure}`
  );
}

function clearSessionCookie(res) {
  res.setHeader('Set-Cookie', `${COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0`);
}

function isAuthenticated(req) {
  const cookies = parseCookies(req);
  return isValidSessionCookieValue(cookies[COOKIE_NAME]);
}

// Express middleware — protects /api/* routes other than /api/login and /api/me.
function requireAuth(req, res, next) {
  if (isAuthenticated(req)) return next();
  res.status(401).json({ error: 'Not authenticated' });
}

module.exports = {
  isAuthenticated,
  requireAuth,
  setSessionCookie,
  clearSessionCookie,
};
