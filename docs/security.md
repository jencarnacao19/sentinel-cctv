# Security

## At a glance

- Passwords are hashed with **Argon2id**.
- Camera credentials are **encrypted at rest** with a key derived from a local
  secret created at install time, stored `0600`. They are never returned by the
  API, never rendered in the interface and never written to a log.
- Every log line passes through a **redaction filter** that removes RTSP
  userinfo, `password=`/`token:` pairs, `Authorization` values and Sentinel API
  tokens.
- Administrator sessions use `HttpOnly; SameSite=Lax` cookies. The separate
  embedded-viewer cookie is `HttpOnly; Secure; SameSite=None` so an HTTPS Home
  Assistant iframe can use it; its narrow capability still cannot reach any
  administration route. A header/WebSocket-subprotocol fallback keeps the
  embedded viewer working when an iOS WebView blocks that cross-site cookie;
  neither mechanism is accepted by administration dependencies.
- Restricted footage access is a separate capability, not a user or API token.
  Its responses omit RTSP URLs, hosts, usernames, recording policy and settings;
  the Home Assistant viewer key arrives in a URL fragment and is never accepted
  by an administration dependency. It may play/download footage and create
  protected exports, but cannot change configuration or delete saved footage.
- CORS is closed by default — the interface is served from the same origin as
  the API.
- Sign-in and token creation are rate limited.
- **Settings → Public login protection** can persistently block a verified
  Cloudflare visitor address after a configurable number of failed
  administrator sign-ins. The default threshold is one and the duration can be
  permanent or timed. Sentinel accepts `CF-Connecting-IP` only from an exact
  `SENTINEL_TRUSTED_PROXY_CIDRS` peer, never from arbitrary LAN traffic. The
  block applies only to sign-in on the configured public hostname; direct LAN
  login remains the recovery path, and the admin panel lists and removes blocks.
- The API binds to the LAN only. Sentinel does not configure port forwarding and
  does not talk to your router. For remote access, use the VPN or reverse proxy
  you already trust.

## The threat model this product actually has

Sentinel is built for one situation: a single device on a home LAN, watched
by one household, that a technical user might be tempted to expose to the
internet. It is **not** built for a multi-tenant deployment, a
security-critical facility, or direct exposure on the public internet, and it
does not pretend otherwise.

**If you take one thing from this page: do not port-forward Sentinel.** Put
it behind a reverse proxy you already run and trust (or a VPN back into your
home network), and terminate TLS there. Two concrete reasons, both grounded in
what is actually in this codebase today, not general advice:

1. **There is exactly one account tier.** The only way a `User` row is ever
   created is the first-run setup endpoint, and it always sets
   `is_admin=True` (`backend/app/auth/service.py`'s `create_admin`). There is
   no code path that creates a second, restricted user. API tokens are separate
   machine principals with `read_only` or `camera_control` scope and never
   satisfy human-administrator dependencies. The separate viewer key is
   not a principal: it can consume enabled-camera footage, operate PTZ already
   detected by an administrator and create protected exports, but cannot
   configure cameras/settings, manage tokens, or delete or unprotect saved
   footage. A compromised administrator session still grants full control;
   a leaked integration token remains limited to its assigned scope and cannot
   create more tokens, administer security or delete stored footage.
2. **The origin is still not an Internet edge.** Sentinel now sends a restrictive
   `frame-ancestors` CSP, MIME-sniffing, referrer and permissions headers, plus
   HSTS when the request is known to be HTTPS. TLS termination, certificate
   renewal and public identity enforcement still belong at the reverse proxy or
   Cloudflare Access. Never expose port `8090` directly.

## What is protected, and how

**Passwords.** Argon2id, tuned for a home server rather than a cloud fleet —
64 MiB memory cost, 3 passes, parallelism 2 (`backend/app/security/passwords.py`)
— comfortably above the minimum recommended parameters while staying well
under a second on modest hardware. A minimum length of 10 characters and a
small list of common passwords are rejected outright. A wrong username still
costs a full hash computation (`authenticate` in `backend/app/auth/service.py`
hashes the submitted password even when the user does not exist), so a
missing account and a wrong password are not distinguishable by response
time.

**Sessions.** Browser sessions are a compact, HMAC-signed token — not a full
JWT library, deliberately: as `backend/app/auth/tokens.py` puts it, *"the
compact signed envelope below is the same construction with none of the
algorithm-confusion surface (the algorithm is fixed, not read from the
token)."* It is carried in an `HttpOnly; SameSite=Lax` cookie, so it is never
reachable from page scripts. `SameSite=Lax` is the first cross-site request
forgery boundary. Unsafe browser requests that authenticate with this cookie
are also checked against their `Origin` and Fetch Metadata; a cross-site POST,
PATCH, PUT or DELETE is rejected before routing. Bearer/API-key clients are
exempt because a hostile page cannot make a browser attach those credentials.
Verifying a session costs no database round trip, which matters because the
frontend holds an open WebSocket and polls snapshots continuously. Set
`SENTINEL_COOKIE_SECURE=true` once Sentinel is served over HTTPS (see
`docs/configuration.md` for what happens if you set it before that).

**API tokens.** Long-lived, meant for Home Assistant. Only a SHA-256 hash is
stored — a full 256 bits of generated entropy does not benefit from a slow
password KDF, and hashing on every one of Home Assistant's requests with
Argon2 would be needlessly expensive (`backend/app/auth/tokens.py`). The
plaintext is shown exactly once, at creation, and every token can be revoked
individually. New tokens default to `read_only`. Select `camera_control` for HA
recording switches, PTZ, privacy and supported local camera operations. Existing
tokens migrate to that control scope to preserve HA operations, not to admin.
Token/credential/system/retention administration and footage deletion remain
human-admin-only. API tokens do not automatically receive the separate viewer
operator grant; an independent valid viewer key retains its own capabilities.

**Public administrator login blocking.** The ordinary in-process limiter stays
in place, and an administrator may additionally persist failed public-login
addresses in SQLite. The threshold is configurable from 1–20 and a block may
be permanent or time-limited. This applies only when the request Host is one of
the configured public viewer hostnames and the TCP peer is inside
`SENTINEL_TRUSTED_PROXY_CIDRS`; only then does Sentinel accept Cloudflare's
`CF-Connecting-IP`. A missing, malformed or untrusted proxy identity fails the
public login closed when protection is enabled. Direct LAN login neither adds
nor observes these permanent blocks, providing a deliberate recovery path.
Existing authenticated sessions, footage viewing and recording are unaffected.
The admin Settings page lists active addresses and can remove one or all of
them. Attempted passwords and successful-login history are never stored in the
block table.

**Restricted footage operator.** This is a separate authorisation boundary, not a user or
API token (`backend/app/viewer/auth.py`). It returns only camera ID, display
name, status, substream availability and last-known codec dimensions; RTSP
addresses, hosts, credentials, retention and motion settings are absent from
its response model. It may use the timeline and playback, stream individual
segments, operate already-detected PTZ, and create/download permanently
protected exports. It has no routes
for camera discovery/configuration, storage settings, token management, export
deletion or removing protection. The main hostname must arrive through Cloudflare Access
with the expected Host and a verified Access JWT. An embedded Home
Assistant dashboard instead uses a random key supplied in the URL fragment,
which browsers do not send in HTTP requests; the frontend stores it only in
session storage, scrubs the address and exchanges it once for a separate
`HttpOnly` viewer-session cookie. On the HTTPS deployment it is
`SameSite=None; Secure`, which is required by the cross-site Home Assistant
iframe without broadening the viewer-only grant. Because iOS WebViews can still
block third-party cookies, the frontend also attaches the same restricted key
as `X-Sentinel-Viewer-Key` to viewer API requests and as a base64url-encoded,
server-validated `sentinel.viewer.*` WebSocket subprotocol. This fallback never
satisfies a principal or administrator dependency, and live WebSocket URLs
contain no key. Cloudflare assertions are independently verified for signature,
issuer, audience and expiry. Keep the origin private even with this validation.

**Viewer accounts.** Password accounts for people who only watch, created by
the administrator. They sign in through the same endpoint and get the same
kind of session as the administrator, but resolving a session into a
principal (`backend/app/auth/deps.py`) returns nothing for them: to every route
outside the viewer they are simply signed out, so no endpoint written before
these accounts existed can serve one. The viewer guard accepts them, and the
only other routes open to them are their own profile, password and appearance.
`tests/test_viewer_accounts.py` sends a viewer account's session to every
route in the API and requires the same answer an anonymous client gets.
Sessions look the account up on every request, so removing an account ends it
everywhere at once; a new password does not end sessions already open.

**Global privacy mode.** A human administrator or a camera-control integration may change this
persistent state; viewers can read only its safe public reason. Enabling it
first commits a fail-safe state, then closes accepted live sockets, stops the
managed go2rtc relay, every recorder and every motion worker, and rejects new
snapshot, live negotiation, discovery, stream-probe and PTZ operations with
`423`. A reboot therefore stays private. Resuming restores the local relay
before recorders and motion workers; if that transition fails, Sentinel rolls
back to the paused state. Historical playback and protected clips remain
available. This controls Sentinel's use of the cameras; it does not cut their
electrical power or prevent some other authorised LAN client from reaching a
camera directly.

PTZ movement is separately rate limited while stop is deliberately never rate
limited. The browser sends a stop when a hold ends and the server has its own
dead-man stop. This gives a safe termination path even if a controlling tab,
phone or network disappears, including on cameras that reject ONVIF's optional
movement-timeout field. If Tapo patrol recovery is enabled for a camera,
Sentinel restarts the selected patrol only after that safety-critical ONVIF
stop. It first reuses the Camera Account credentials already encrypted at rest.
Firmware that requires `admin` plus the TP-Link cloud password can use a
separate write-only control credential, encrypted with the same local Fernet
key and used only for the camera's proprietary LAN request. An authentication
failure backs off for 30 minutes to avoid prolonging the camera's own temporary
lockout, and never changes the successful stop response.

Named positions and native patrol use only the validated ONVIF service pinned
to the configured camera host. When a camera advertises preset tours, this
standard LAN path takes priority over the proprietary Tapo adapter. A failed
native-tour resume may use the separately configured Tapo LAN fallback, but
neither failure can roll back or delay the preceding Stop. No cloud API,
identity refresh or external availability check exists in the PTZ request path.

**Browser response hardening.** Every response gets `X-Content-Type-Options:
nosniff`, a strict referrer policy, a restrictive permissions policy,
cross-origin resource isolation and a full CSP. Scripts, styles, images, media,
workers and connections are restricted to the resources Sentinel actually
uses; object embeds and alternate base URLs are disabled. `frame-ancestors`
contains only Sentinel itself plus explicitly configured HTTPS origins or an
exact private LAN HTTP origin for the Home Assistant app. The pre-paint theme
script is served as a same-origin file, so `script-src` needs no inline-script
exception. HSTS is emitted only when HTTPS is observed directly or through
`X-Forwarded-Proto`, avoiding broken local HTTP development. `X-Frame-Options`
is deliberately not sent because modern `frame-ancestors` is needed to allow an
exact Home Assistant origin without allowing every site.

**Rate limiting.** An in-process sliding-window limiter
(`backend/app/auth/ratelimit.py`) covers sign-in (5 attempts/minute per
client address), token creation (10/5 minutes) and CPU-heavy endpoints such as
stream probing (20/minute) — generous for a human, useless for a script.
It keys on the raw socket address and deliberately ignores
`X-Forwarded-For`, because trusting a client-supplied header unconditionally
would let anyone forge their own bucket. The consequence: behind a reverse
proxy, every client sharing that proxy shares one bucket too. On a home LAN
this is an acceptable trade, not an oversight.

**Camera credentials.** Encrypted at rest with Fernet, using a key derived
(HKDF-SHA256) from a 32-byte secret generated once at install time and stored
with `0600` permissions next to the database
(`backend/app/security/crypto.py`). The plaintext password is only ever
decrypted in-process, to build an FFmpeg argument list or go2rtc's
configuration — never returned by the API, never rendered in the interface,
never written to a log. go2rtc's own generated configuration file
(`go2rtc.yaml`) necessarily contains camera passwords in plaintext — go2rtc
has no other way to authenticate to an RTSP source — so that file is written
with restricted permissions, excluded from diagnostics and backups, and never
served over HTTP (`backend/app/streaming/config.py`).

**go2rtc's own surface.** go2rtc's built-in API and RTSP listeners bind to
loopback only (`127.0.0.1` by default) and are never reachable from the LAN;
its admin API has no authentication of its own and could otherwise be told to
open an arbitrary source, including a local file, so the browser is never
allowed to talk to it directly. Every live-view request goes through a
Sentinel endpoint that authenticates first and only ever forwards to the one
stream name that belongs to the camera being requested
(`backend/app/streaming/router.py`). The one exception is go2rtc's WebRTC
port, which does bind on every interface because the browser's peer
connection has to reach it directly for media — per its own configuration
comment, that port *"carries media only, never control"*: negotiating a new
stream still has to go through Sentinel's authenticated
`/streams/{id}/webrtc` endpoint first.

**Logging.** Every Sentinel logger is attached under the root logger's
redaction filter (`backend/app/logging_config.py`), which strips RTSP
userinfo, `password=`/`token:`-style key-value pairs, `Authorization` header
values and Sentinel's own `sctv_…` API tokens from both the log message and
every structured field attached to it — including a third-party library that
happens to log a raw RTSP URL. The same redaction function is applied a
second time to diagnostics bundles and to HTTP error bodies (validation
errors specifically avoid echoing the submitted value back, since that value
might be a camera password). The in-process event bus
(`backend/app/events/bus.py`) goes further for anything it broadcasts: keys
that look sensitive are dropped from a published payload entirely rather than
masked, on the reasoning that a subscriber has no use for the fact that a
password merely existed.

**Path handling.** Every path derived from a database row or a user-supplied
identifier — a recording download, an event's snapshot or thumbnail, a
restored backup archive's internal file names — is resolved and checked
against its allowed root before it is ever opened
(`backend/app/security/paths.py`). A restore archive is additionally checked
member-by-member for the classic zip-slip pattern (`../` or an absolute path
inside the zip) before anything is extracted.

**Stream URLs.** Only `rtsp://` and `rtsps://` are accepted for a camera's
stream address (`backend/app/cameras/rtsp.py`); `file://` in particular would
otherwise turn "add a camera" into "read an arbitrary file the process can
reach." This is deliberately **not** a general SSRF blocklist against private
or local addresses — Sentinel is a LAN application and has to be able to
reach `192.168.x.x` and `10.x.x.x` hosts to do its job at all. The scheme
allow-list, combined with FFmpeg always being invoked as an argument list and
never through a shell, is the actual boundary.

**Backups.** A configuration backup excludes camera passwords entirely (they
are encrypted with a secret that never leaves the machine, so an archive
containing them would be either useless or dangerous) but keeps user
password hashes and API token hashes, which are one-way and safe to carry
over so the administrator account survives a restore
(`backend/app/system/backup.py`). Restoring keeps the previous database
alongside the new one rather than overwriting it outright, so restoring the
wrong archive is recoverable.

## What is not protected

Said plainly, so nothing here is a surprise later:

- **Two human roles, not per-camera permissions.** There is one administrator
  and any number of viewer accounts; a viewer account sees every camera.
  Machine integration scopes are separate, and the footage operator remains a
  distinct shared grant.
- **Access SSO requires explicit configuration.** Sentinel verifies RS256,
  issuer, application audience and expiry using bounded, cached public keys
  from the configured Access team. Unconfigured, forged, expired or wrong-app
  assertions fail closed; normal login and separate viewer keys remain local.
  The origin still belongs behind its trusted proxy, not on the Internet.
- **No synchroniser CSRF token.** Cookie-authenticated mutations instead use
  the combination of `SameSite`, Origin and Fetch Metadata validation. Direct
  non-browser clients without those browser headers remain supported.
- **The general rate limiter is per-process and resets on restart.** Public
  administrator login can additionally use the persistent, verified visitor-IP
  blocks described above. Other expensive endpoints remain bucketed by raw
  socket address, so clients behind the same reverse proxy share those buckets.
  This is not a substitute for Cloudflare Access or an edge WAF.
- **`GET /api/v1/system/health` is intentionally unauthenticated** (it is
  what Docker's own healthcheck calls), and its response includes coarse
  operational detail — whether recorders are running, how many are expected
  — to anyone who can reach the port. Nothing in it is a credential, but it
  is not nothing either.
- **A WebSocket client that cannot set an `Authorization` header falls back
  to a `?token=` query parameter** (`backend/app/auth/deps.py`). A token or
  session value passed this way can end up in an intermediate proxy's access
  log if you put a logging reverse proxy in front of the WebSocket endpoint.
  The bundled frontend never needs this path — it authenticates with the
  cookie the browser already attaches to the handshake — so this only matters
  for a custom client.
- **Nothing in this build has been reviewed by anyone outside this project.**
  No third-party security audit has been done.

If you find a vulnerability, follow the [security policy](../SECURITY.md) for
the security maintainer and confidential reporting instructions.
