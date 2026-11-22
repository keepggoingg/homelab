// n8n external hook: single sign-on through Cloudflare Access.
// Cloudflare Access sits in front of n8n.$DOMAIN and adds a signed JWT
// (Cf-Access-Jwt-Assertion) to every request it lets through. When that JWT is
// valid (signature from the team's certs, this app's AUD, not expired) and its
// email belongs to an n8n user, we issue the normal n8n session cookie, so the
// n8n login form never appears. Requests without a valid Cloudflare JWT (e.g.
// LAN requests straight to Traefik) fall through to n8n's own login untouched.
// Enabled by EXTERNAL_HOOK_FILES; config via CF_ACCESS_TEAM and CF_ACCESS_AUD.
'use strict';
const https = require('https');
const crypto = require('crypto');
const N8N = '/usr/local/lib/node_modules/n8n';
const jwt = require(`${N8N}/node_modules/jsonwebtoken`);
const { Container } = require(`${N8N}/node_modules/@n8n/di`);
const { UserRepository } = require(`${N8N}/node_modules/@n8n/db`);
const { AuthService } = require(`${N8N}/dist/auth/auth.service`);
const { AUTH_COOKIE_NAME } = require(`${N8N}/dist/constants`);

const TEAM = process.env.CF_ACCESS_TEAM;          // e.g. bitter-bread-6b5f
const AUD = process.env.CF_ACCESS_AUD;            // Access app AUD tag
const ISSUER = `https://${TEAM}.cloudflareaccess.com`;
const log = (...a) => console.log('[cf-access-sso]', ...a);

let keys = {}, keysAt = 0;
function fetchJson(url) {
  return new Promise((resolve, reject) => {
    https.get(url, (res) => {
      let body = '';
      res.on('data', (c) => (body += c));
      res.on('end', () => { try { resolve(JSON.parse(body)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}
async function keyFor(kid) {
  if (!keys[kid] || Date.now() - keysAt > 3600e3) {
    const { keys: jwks = [] } = await fetchJson(`${ISSUER}/cdn-cgi/access/certs`);
    keys = Object.fromEntries(jwks.map((k) => [k.kid, crypto.createPublicKey({ key: k, format: 'jwk' })]));
    keysAt = Date.now();
  }
  if (!keys[kid]) throw new Error('unknown Cloudflare key id');
  return keys[kid];
}
async function verifiedEmail(assertion) {
  const { header } = jwt.decode(assertion, { complete: true }) || {};
  if (!header || !header.kid) throw new Error('malformed assertion');
  // Signature (this account's keys) + AUD (this Access app) are what prove authenticity. The issuer is
  // only sanity-checked: Cloudflare keeps the old team domain as issuer for a while after a rename.
  const claims = jwt.verify(assertion, await keyFor(header.kid), { algorithms: ['RS256'], audience: AUD });
  if (!/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/.test(claims.iss || '')) throw new Error('unexpected issuer ' + claims.iss);
  if (!claims.email) throw new Error('no email in assertion');
  return String(claims.email).toLowerCase();
}
function readCookie(req, name) {
  const m = (req.headers.cookie || '').match(new RegExp(`(?:^|;\\s*)${name}=([^;]+)`));
  return m ? decodeURIComponent(m[1]) : undefined;
}

module.exports = {
  n8n: {
    ready: [
      async function (server) {
        if (!TEAM || !AUD) return log('CF_ACCESS_TEAM / CF_ACCESS_AUD not set, SSO disabled');
        const auth = Container.get(AuthService);
        const users = Container.get(UserRepository);

        async function sso(req, res, next) {
          try {
            const assertion = req.headers['cf-access-jwt-assertion'];
            // Only the SPA's API calls matter; static files and webhooks pass through.
            if (!assertion || !req.path.startsWith('/rest/') || req.path === '/rest/logout') return next();
            const existing = readCookie(req, AUTH_COOKIE_NAME);
            if (existing) {
              try { await auth.validateCookieToken(existing); return next(); } catch { /* expired or revoked: re-issue */ }
            }
            const email = await verifiedEmail(assertion);
            const user = await users.findByEmailWithRole(email);
            if (!user || user.disabled) { log(`no active n8n user for ${email}`); return next(); }
            const token = auth.issueJWT(user, false);
            res.cookie(AUTH_COOKIE_NAME, token, { maxAge: auth.jwtExpiration * 1000, httpOnly: true, sameSite: 'lax', secure: true });
            // Let this same request authenticate too (cookie-parser runs after us).
            const others = (req.headers.cookie || '').split(/;\s*/).filter((c) => c && !c.startsWith(`${AUTH_COOKIE_NAME}=`));
            req.headers.cookie = [...others, `${AUTH_COOKIE_NAME}=${token}`].join('; ');
            log(`signed in ${email} via Cloudflare Access`);
          } catch (e) {
            log('assertion rejected:', e.message);
          }
          next();
        }

        server.app.use(sso);
        const stack = server.app.router.stack;
        stack.unshift(stack.pop()); // run before n8n's own middleware and routes
        log(`enabled for team ${TEAM}`);
      },
    ],
  },
};
