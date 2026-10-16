# Remote access and the footage console

How to reach Sentinel from outside your network and how to embed it in a Home
Assistant Webpage dashboard without exposing administration.

Sentinel can make `/` a footage console while keeping every configuration route
under `/admin` behind the administrator login. This is deliberately not another
account system. The operator grant can list enabled cameras, watch live video,
operate cameras whose ONVIF PTZ support has been detected, use the timeline and
playback, download footage and create permanent saved clips. It cannot
discover/add/edit/delete cameras, change retention or motion settings, manage
credentials/tokens, delete exports or remove their protection.

For the main remote hostname, put Sentinel behind Cloudflare Access and enable
only that exact hostname:

```dotenv
SENTINEL_VIEWER_ENABLED=true
SENTINEL_VIEWER_TRUSTED_HOSTS=cctv.example.net
SENTINEL_VIEWER_CLOUDFLARE_ISSUER=https://your-team.cloudflareaccess.com
SENTINEL_VIEWER_CLOUDFLARE_AUDIENCES=your-application-audience-tag
```

The exact hostname and a signed Cloudflare Access JWT must be present. Sentinel
checks the configured issuer, audience, signature and expiry against that team's
public signing keys. Missing verification settings disable this grant, not the
normal Sentinel login or independent viewer key. Obtain the team domain and
Application Audience (AUD) from your own Access application; do not invent them.
The origin must remain private behind the tunnel; never expose port `8090` directly.

The native Home Assistant integration should use Sentinel's LAN URL directly,
for example `http://<sentinel-lan-ip>:8090`. An HTTPS Home Assistant page cannot
embed that HTTP address because browsers block mixed content. For the Webpage
dashboard, create a more-specific Cloudflare Access application that bypasses
only `/embed*`, `/assets/*`, `/sw.js`, `/api/v1/viewer/*` and
`/api/v1/streams/*`. Sentinel's random viewer key remains mandatory for the
viewer APIs. The main `/` and every administration route stay behind the normal
Cloudflare Access application. This needs no router change or split DNS.

Live video uses MSE on desktop browsers. Safari/Home Assistant WebViews first
try protected WebRTC for low latency on the LAN; if a mobile network cannot
reach the private WebRTC media candidate, the player automatically switches to
same-origin HTTPS HLS through the existing tunnel. This fallback opens no
router port, does not add a TURN/cloud-video dependency and exposes neither the
go2rtc interface nor the long viewer key. All three paths retain the same
viewer-only authorisation boundary.

Generate a separate random key of at least 32 characters for the embedded
Webpage dashboard and pass it as a deployment secret:

```dotenv
SENTINEL_VIEWER_KEY=<random value, never commit this>
```

Configure the dashboard URL as
`https://cctv.example.net/embed#viewer_key=<same value>&embed=1`. The fragment is not
sent to Sentinel, Cloudflare or HTTP logs. The frontend saves it only for that
browser session, removes it from the visible address and exchanges it once for
an `HttpOnly` viewer-session cookie. To survive iOS and Home Assistant WebViews
that block third-party cookies, restricted API calls also send the key in the
viewer-only header and live video uses a validated WebSocket subprotocol. The
key never appears in an HTTP or WebSocket URL.
Set `SENTINEL_FRAME_ANCESTORS` to the exact HTTPS Home Assistant origin and, if
the mobile app uses it on Wi-Fi, the exact private LAN HTTP origin. Do not
create an unprotected public alias: without the viewer key or Access assertion,
the footage API returns `401`.
