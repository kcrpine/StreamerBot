# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

StreamerBot streams media into a TeamTalk 5 voice channel. One Docker container per bot, many bots per
host, managed from a bash CLI. It is a rewrite of TTMediaBot (`gumerov-amir/TTMediaBot` via
`JoaoDEVWHADS/TTMediaBot`), currently mid-migration: `README.md` still describes the old project and is
rewritten in the final phase.

**Primary users are blind.** This is not a footnote — it constrains the web portal, the chat messages
and the CLI, and it has already changed real design decisions. See "Accessibility is binding" below.

The full plan, the phase order, and a record of what each phase actually found is in
[.claude/plans/streamerbot-plan.md](.claude/plans/streamerbot-plan.md). **Read it before starting
work**; it carries decisions and their reasoning that are not recoverable from the code.

## The tests do not run on a Windows host

`bot/` imports `TeamTalkPy` (needs `libTeamTalk5.so`/`TeamTalk5.dll`) and `mpv.py` (needs libmpv).
Neither ships in the repo. Run the suite inside the image instead:

```bash
docker run --rm -v "$PWD:/work" -w /work --entrypoint bash streamerbot:test -c 'python -m unittest discover -s . -p "test_*.py"'
```

A single test:

```bash
docker run --rm -v "$PWD:/work" -w /work --entrypoint bash streamerbot:test -c 'python -m unittest test_engine_dispatch.EngineActivationTests.test_switching_stops_the_outgoing_engine_exactly_once -v'
```

`tools/ci_test_report.py` runs the same suite and emits a per-test Markdown/text report; CI uses it, and
it is more readable than raw unittest output when several tests fail.

**That command is not the whole suite, and it says so nowhere.** `tests/deployment/` and
`test_update_shared_youtube.py` drive the shell scripts and skip inside the image — not because
`.dockerignore` excludes those scripts, which the `-v "$PWD:/work"` mount puts back, but because the
image has no `jq`. They skip rather than fail, so an in-image run reports a clean pass having executed
none of them: 646 tests pass in the image while all 181 deployment tests sit out. That is how a stale
assertion in `tests/deployment/test_port_allocation.py` failed CI on five consecutive pushes while the
in-image job stayed green throughout.

The two halves are disjoint, so the full suite is both commands. The host half needs `jq` and `bash`
and cannot run `bot/`; the image half needs only Docker and cannot run the deployment tests:

```bash
python -m unittest discover -s tests/deployment -t . -p "test_*.py"   # 181 tests, ~90s
python -m unittest test_update_shared_youtube                          # 3 tests
```

`.githooks/pre-push` runs both before every push, so a green hook means a green CI. Missing Docker, a
missing image or a missing `jq` is a warning rather than a refusal, so a collaborator without them can
still push; only a real failure stops one. Bypass with `git push --no-verify`.

A Windows host can run the deployment half only where `bash` and `jq` are on PATH, such as Git Bash.

### Docker lives in WSL on this Windows host

There is no Docker on the Windows side: `docker` is on neither the Git Bash nor the PowerShell
PATH, and Docker Desktop is not installed. The engine runs natively inside WSL — Ubuntu 24.04,
`/usr/bin/docker`, Engine 29.8.1 on `overlayfs`, started by systemd as `docker.service`. The image
half of the suite therefore has to be driven through WSL:

```bash
wsl -e bash -lc 'cd "/mnt/c/Users/kcrpi/Documents/teamtalk tv streamer and music bot/TTMediaBot" && docker run --rm -v "$PWD:/work" -w /work --entrypoint bash streamerbot:test -c "python -m unittest discover -s . -p \"test_*.py\""'
```

No `sudo`: the WSL user is in the `docker` group, so the socket is reachable directly. The Windows
path is visible from WSL under `/mnt/c/...`, so no second clone is needed — but the bind mount
crosses the 9p filesystem boundary and is slower than a clone kept inside the distro.

This changes what `.githooks/pre-push` actually checks, depending on where you push from:

- **From Git Bash or PowerShell** the hook runs *neither* half. There is no `docker`, and there is no
  `jq` anywhere on the Windows side either, so both halves degrade to warnings and the push proceeds
  having tested nothing at all. Observed on the push that added this note: two warnings, then
  "OK. Pushing."
- **From inside WSL** both halves run for real: the distro has `jq` at `/usr/bin/jq` and the
  `streamerbot:test` image is already built there, so neither half degrades to a warning.

Push from WSL when you want the hook to mean what it claims.

## Building the image

Build args are mandatory — the Dockerfile fails loudly on an empty SDK URL rather than producing a
broken image:

```bash
set -a; . ./project.env; set +a
docker build --build-arg TTSDK_URL_X86_64="$TTSDK_URL_X86_64" --build-arg TTSDK_URL_ARM64="$TTSDK_URL_ARM64" --build-arg GO_LIBRESPOT_VERSION="$GO_LIBRESPOT_VERSION" -t streamerbot:test .
```

`streamerbot.sh` and `update.sh` both assemble this from `IMAGE_BUILD_ARGS`; if you add a build arg, add
it in both.

## project.env is the single source of truth

Repository, image name, TeamTalk SDK URLs, pinned go-librespot version, update interval. Shell scripts
source it; `bot/app_vars.py` parses it with a stdlib parser tolerant of a missing or malformed file.
Never hardcode a repo URL, image name or SDK version anywhere else.

Format is strict: `KEY=VALUE`, no quotes (the Python parser takes them literally), no shell expansion.
Note that keys contain digits (`TTSDK_URL_X86_64`) — a `[A-Z_]+` filter silently drops them, which
already broke CI once.

## Architecture

### Player is a transport over engines, not an mpv wrapper

mpv plays a URL. The Spotify daemon and the browser are *already producing audio into the sink* — there
is no URL to hand anyone. So `bot/player/__init__.py` dispatches to a `PlaybackEngine`
(`bot/player/engines/`), of which exactly one is active at a time.

- `Track.engine` (`"mpv"`, `"librespot"`, `"browser"`) selects it; `TrackType.External` marks tracks
  with no resolvable stream URL.
- `_activate_engine()` stops the outgoing engine — neither the browser nor the Spotify daemon goes quiet
  on its own.
- End-of-track funnels through `_advance_after_end()` from both mpv's `on_end_file` and engines'
  `on_engine_end`, so queue and play-mode behaviour is identical regardless of engine.
- **Engines never touch `player.state`.** Only `Player` mutates it, which is what keeps
  `TTPlayerConnector` (voice transmission, status text) working unchanged.

`_play()` accepts a `Track` *or* a URL string. The string form is the YouTube stream-refresh retry,
which already holds a freshly resolved URL and must not go back through `Track.url`. mpv's own event
handling stays on `Player` rather than moving into `MpvEngine`, because `test_player_stream_retry.py`
pins `on_end_file`, `self._player` and `_play(url_string)` onto `Player`.

Volume fading lives in `Player`, not the engine: `play()` sets volume directly, so a fading
`engine.set_volume` would slide the volume on every track start.

### The YouTube bridge is a shared Node process

`youtube_bridge/server.mjs` runs in **one container serving every bot** (`streamerbot-youtube`), using
youtubei.js. Python talks to it over HTTP via `bot/services/youtube_bridge.py`.

- **`bot_id` is the containment boundary.** Every request carries one; it is validated against
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and joined under `BOTS_ROOT`. That regex is load-bearing — it is
  what stops one bot reaching another's YouTube session. Do not relax it.
- The `/bots` mount must be **`:rw`**; the bridge keeps a session cache in `bots/<id>/youtube_auth/`. Both scripts
  check the mount's RW flag before deciding a container is current, because a read-only container is
  healthy and useless.
- The process installs `unhandledRejection`/`uncaughtException` handlers on purpose. Node makes an
  unhandled rejection fatal, and one bot's abandoned OAuth sign-in once killed YouTube for every bot on
  the host. That flow is gone; the rule is about the shared process, not the flow.

### YouTube signs in as a real browser session (Phase 9)

The OAuth device code is **retired**. YouTube answered 400 to every OAuth-authenticated player request
and refuses anonymous playback from datacenter addresses, so a VPS bot could sign in and play nothing.
What it still serves is a browser session: cookies plus a proof-of-origin token bound to the account's
**DataSync ID** (not `visitorData`, which is the signed-out binding — a token bound to the wrong one is
silently ignored and looks exactly like not being signed in).

- The **bot** owns the session. `bot/services/web/youtube.py` signs Google in through the bot's Chrome
  profile `data/browser/yt/`; `bot/modules/youtube_session_keeper.py` exports it to
  `data/youtube_auth/cookies.txt` (0600) with `session.json` (DataSync ID), reloads youtube.com every
  `services.yt.session_refresh_hours` so Google keeps rotating it, and marks it ended when YouTube's
  own `ytcfg.LOGGED_IN` says so. Cookie expiry dates mean nothing; only that check does.
- The **bridge** only reads those files. Its session cache key includes the cookie file's mtime, so a
  rotation rebuilds that one bot's session and nothing restarts. **Write cookies.txt only when the
  cookies changed** (`fingerprint` ignores expiry) or every refresh throws away the warm session.
- Import (`/import/yt`) is a first-class route, not a fallback nobody tests: Google often refuses an
  automated Chrome, and arm64 has no Chrome at all.
- Commands run on the main loop, so a refresh never blocks there. A request refused with
  `LOGIN_REQUIRED` gets one "renewing" message, a background refresh (at most every ten minutes), and
  then exactly one follow-up. `yl` survives only as status and sign-out.
- Written against Google's documented flow; the sign-in selectors and the DataSync binding are **not yet
  measured against the live site**. When it fails, look at the logged page path before changing code.

**Two things about resolving a stream that cost a day each.** Both are in `media.mjs`'s
`planPlaybackAttempts`, which is where the client chain lives and is pure so it can be tested:

- **`ClientType` is not the vocabulary `getBasicInfo({client})` speaks.** `ClientType` is for
  `Innertube.create({client_type})`. `getBasicInfo`'s `client` option is validated against
  `Constants.SUPPORTED_CLIENTS`, which holds the enum's **keys** (`'TV_EMBEDDED'`), while
  `ClientType.TV_EMBEDDED` is its **value** (`'TVHTML5_SIMPLY_EMBEDDED_PLAYER'`). Passing the enum
  throws `Invalid client: …` inside the process, so that fallback silently never ran. The two
  parameters look interchangeable and are not.
- **A signed-in session must be able to fall back to an anonymous one.** YouTube answered **400** to an
  OAuth-authenticated player request that it served anonymously, so signing in for age-restricted
  content broke *all* playback, and a session Google has just ended fails the same way. Authenticated
  attempts come first — they are the only ones that can return age-restricted or member content, and
  the only ones a datacenter address is served at all — and the anonymous session is built lazily. Search
  is unaffected either way, so "search works but nothing plays" is the signature of this class of bug.

A chain of fallbacks is only a fallback if the entries differ. The original read
`['YTMUSIC', 'MWEB', ClientType.TV_EMBEDDED]` and was one real client: the caller rewrote `'YTMUSIC'`
to MWEB and the third was invalid. `youtubei.js` is pinned to `#main`, so this is a moving target —
when playback breaks, probe which clients resolve *today* before changing the order.

### mpv never fetches a googlevideo.com URL directly — `bot/services/stream_proxy.py`

Resolving a stream is not the same as being able to play it. A resolved `videoplayback` URL is handed
to mpv, which asks for the whole remaining file in one request (`Range: bytes=0-`, ordinary progressive
download). Google's CDN answers a bare **403** to a single request past some size — measured directly
against real resolved URLs, one video tolerated up to 1,000,000 bytes and 403'd at 2,000,000; another
403'd anywhere past ~950,000. **The cutoff is not a fixed constant** — it varies per video/session, so a
single hardcoded chunk size will eventually guess wrong for some track. `on_end_file`'s stream-refresh
retry (`bot/player/__init__.py`) treated this as a normal playback error, re-resolved to a brand-new
URL, hit the identical 403 again, and — since a URL that resolves fine but 403s on the CDN produces no
`ServiceError` for anything upstream to catch — silently advanced to the next track instead. Set loose
on an autoplay queue that reads as "skips through 15+ tracks in under two seconds," which looks exactly
like the bot picking bad tracks rather than every track failing the same way.

**A day was spent on the wrong culprit first.** Debian's ffmpeg has no OpenSSL support at all
(`ffmpeg -version`'s build config shows `--enable-gnutls`, never `--enable-openssl` — that needs
`--enable-nonfree`, which Debian's package deliberately omits), and the same URL that 403'd through
mpv/ffmpeg succeeded every time fetched with `curl` or Python's `requests` — both OpenSSL-based. That
looked conclusive: rebuild ffmpeg with OpenSSL, done. It would have been a heavy, fragile image change
for nothing, because the actual `requests` tests up to that point had all used a small explicit `Range`
without noticing it — GnuTLS was never the variable, request size was. Probe the byte-range cutoff
directly before reaching for a TLS-backend explanation; it is cheaper to rule out and it is what the CDN
is actually enforcing.

The fix: `bot/services/stream_proxy.py` runs a loopback-only HTTP relay (like `auth_portal` and
go-librespot's API port, one more per-bot port — `player.stream_proxy_port`, default 4420, in the same
`assign_unique_bot_ports` machinery in `streamerbot.sh`). `yt.py`/`ytm.py` register the real resolved
URL with it and hand mpv a `http://127.0.0.1:<port>/<token>` URL instead. The relay fetches upstream in
bounded windows comfortably under the lowest cutoff seen so far, concatenating them into one continuous
response so mpv never knows chunking happened; a window that still 403s is halved and retried rather
than failing the whole track, since the cutoff itself is a moving target, not a wall.

**Resolved 2026-09-21 ([055]): there are two proof-of-origin tokens and they are bound differently.**
The *player* token is session-bound (`visitorData` signed out, `DATASYNC_ID` signed in) and is what
gets a datacenter address past `LOGIN_REQUIRED` at resolve time. The *GVS* token — the `&pot=` on the
`videoplayback` URL, which Google's CDN checks — is bound to the **video ID**. The bridge minted one
session-bound token and used it for both, and a token whose binding does not match is not rejected, it
is **ignored**: the URL behaved byte for byte like one carrying no token at all. Swapping a video-bound
token into an otherwise identical URL turns the 403s into 206s, and an open-ended range from mid-file
then serves the whole remainder in one request. Measured on three videos. **Do not "simplify" these two
back into one token** — each fixes a different half of playback and neither substitutes for the other.

**The measurement that led there, kept because it is how the symptom reads before the cause is known.**
Probing resolved URLs directly from the host, a short video (213s, 3.4MB) is served in full — every
window, non-zero start offsets, an open-ended range, all fine. A long one is not: for a
1772s track and a 5251s track alike, **only the first ~1,024,000 bytes are served and everything past
that is a flat 403**, whichever offset or window size asks for it. That is roughly 60 seconds of Opus.
Binary search put the boundary between 1,015,625 and 1,031,250 bytes on a fresh URL. It is not about
range sizing, not about IP family (forcing IPv4 and IPv6 behave identically, though the URL is signed
for the host's IPv4 and the relay reaches the CDN over IPv6), and not per-request — it is an absolute
position in long-form content. Signing in still matters and is not sufficient: a bot with no YouTube
session cannot resolve these videos at all (`LOGIN_REQUIRED` on every client), while a signed-in one
resolved them and was then capped. "The bot plays a minute of every long video and moves on" is the
signature of the missing GVS token above — the remedy was in the attestation, never in
`_UPSTREAM_CHUNK_BYTES`. The relay still windows, which is now belt and braces rather than the fix: it
keeps a track playing if a token ever goes missing again, instead of losing everything past the first
minute silently.

**A live broadcast is not a file, and the relay's byte windows are wrong for one ([056]).** Asking a
live URL for a byte range does not fail in any way a caller can see: YouTube answers **`206` with a
`Content-Length` and then sends no body at all**. Measured three times running — 206, "700000 bytes to
follow", zero bytes, connection dropped after ~36s; held open for 122 seconds, still zero. So the relay
promised mpv a body that never arrived, mpv raised no error because the stream was open and valid, and a
real broadcast sat silent in kuhao's log for **twelve minutes and twenty-four seconds** across five
`start-file` events and four stream refreshes with no warning at any level. Neither existing net fires:
the short-EOF check wants an EOF that never comes, and the truncation warning wants a 403, not a 206.

`&sq=N` is how the live endpoint is actually addressed — one complete segment per request, and a request
for the segment past the live edge **blocks until that segment exists**, which is the real-time pacing a
broadcast wants and not a stall to time out. `stream_proxy.py` splits on the URL (`live=1`/`noclen=1`,
which YouTube sets itself) and walks segments for live, windows bytes for files. Two rules if you touch
it: a live response carries **no `Content-Length` and no `Accept-Ranges`**, because a broadcast has
neither and a length is exactly the promise that hid this for twelve minutes; and **re-resolving a live
URL is never the remedy** — every fresh URL behaves identically, which is why four refreshes changed
nothing.

**The failure was invisible, which is what made it expensive.** When a later window is refused the relay
has already sent mpv a `Content-Length` for the whole file, so all it can do is stop writing. ffmpeg
reports "Stream ends prematurely", reconnects a few times at the offset it reached, and then fires
end-file with reason **EOF** — measured against a real libmpv, a 60s file truncated after 0.4s gives
`reason=0`, the same event a finished track gives. `Player.on_end_file` only special-cased `ERROR`, so
every truncated track went straight to `next()` with no refresh, no error and no log line at any level:
282 tracks in 451 seconds, reading as the bot choosing bad tracks rather than every track failing the
same way. `Player` now keeps the last `time-pos`/`duration` (mpv clears both before end-file) and judges
an EOF against the track's own length; the relay logs the upstream status and offset behind the
truncation. **A relay that cannot signal failure downstream has to at least say so in the log** — and
note that a track playing its opening minute and stopping cannot be fixed by re-resolving, since the
fresh URL carries the same cap.

### Per-bot isolation is a requirement, not an accident

Each container is created with `-v "${BOTS_ROOT}/${bot}:/home/streamer/StreamerBot/data"`, so every
credential path (`data/secrets/`, `data/youtube_auth/`, `data/browser/`, `data/librespot/`) resolves
inside that one bot's directory. **Nothing credential-related may live outside `data/`.** Two bots on
one host must never share a streaming account.

Bot directories are owned by uid 1000 (the container user). `update.sh` deliberately prunes `bots/`
from its repo-wide `chown`/`chmod` pass — a blanket `chmod -R 777` there once left credentials
world-writable and locked the container out.

**Bots run with `--network host`, so any port in `config.json` must be unique per bot.** Three are:
`auth_portal.port` (4419), `services.sp.api_port` (3678), and `player.stream_proxy_port` (4420, the
local relay described above). All three were originally written as the same constant into every bot, so
the first bot to start took them and every other bot lost its portal *and* its Spotify daemon. Neither
failure named itself — a portal that failed to bind was indistinguishable from one switched off, so `li`
blamed the configuration, and go-librespot's output went to `/dev/null` so its restart loop logged no
exit code. The stream proxy fails closed instead (falls back to fetching googlevideo.com directly), so
a clash there reads as "YouTube skips sometimes" rather than an obvious startup failure. `streamerbot.sh`
now allocates per bot (`assign_unique_bot_ports`) on create, on restore, and automatically before Start
All and Restart All, with `--repair-ports` for existing bots.

Two rules if you touch that allocation: it must be **idempotent**, since it runs before every start; and
a bot's **own** live listener is not a clash. Testing "is this port in use" with `ss` catches the bot's
own running portal and moves it on every restart, which breaks the firewall rule and `public_url` the
user set up. That is why there are two predicates — `port_claimed_by_another_bot` (config only, for a
port a bot already has) and `port_free_for_new_bot` (also checks listeners, for choosing a new one).

**If you add a new listening port, add it to both.** And remember a free port is only half of
reachability: the port still has to be allowed through the host's firewall and forwarded by anything in
front of it, which produces the same "link does not open" symptom for an entirely different reason.

### Auth portal and secrets

`bot/modules/auth_portal.py` is a stdlib `ThreadingHTTPServer` with a hand-rolled router;
`bot/modules/portal_pages.py` builds the HTML **from translated fragments in Python** so Babel's
extractor sees the strings. There are no template files.

There is no login page: tokens are minted only by a TeamTalk command from a user who already passed
`CommandProcessor.check_access`. A missing token returns 404 and a wrong one 410 — 403 would confirm
there is something worth attacking.

`bot/auth/store.py` encrypts each field separately over Fernet. `bot/auth/redaction.py` installs a
filter on the root logger **and every handler** — a filter on the logger alone does not run for records
propagating up from child loggers, which is exactly where library tracebacks come from.

## Accessibility is binding

`.claude/settings.json` wires hooks that route user-facing work through the `accessibility-lead` agent
*before* it is written. Do not treat this as ceremony.

The Phase 3 portal review overturned five decisions the plan had already marked settled. The worst: the
OAuth device code was specified as an `aria-hidden` paragraph, which is absent from the screen reader's
virtual buffer — a blind user could not have selected or copied the one string they must transcribe onto
another device. It is now a `readonly` input.

Concrete rules that are easy to regress and are covered by tests in `test_auth_portal.py`:

- Device code is a `readonly` input, never `aria-hidden`, never `disabled`.
- Codes go in the body, never the `<title>` — synths mangle `BCDF-GHJK` as a word, differently per synth
  across seven shipped languages, and a title is announced once.
- OTP field is `type="text"` + `inputmode="numeric"`, never `type="number"` (announces as a spinbutton,
  drops leading zeros). No `maxlength` on OTP or password fields.
- Service names are visible in controls, not hidden spans — a hidden span risks `ConnectNetflix` in the
  accessible name and hands translators a bare verb with no object, unbuildable in Turkish.
- Controls that navigate are `<a>`; only the disconnect POST is a `<button>`, and it must stay POST.
- No `role="alert"` on server-rendered error summaries; no `meta refresh`; no modals.
- `ar` ships, so RTL is real: logical CSS properties, `dir="ltr"` on code inputs.

Chat messages follow the same logic: one message at start and one at finish, never a self-rewriting
percentage, which a screen reader reads in full on every tick.

## Translations

Gettext domain is `StreamerBot`. Every user-facing string goes through `translate(...)`. After changing
strings run `python tools/compile_locales.py` and commit the updated `.pot` and `.po` files. Seven
locales ship (ar, es, hu, id, pt_BR, ru, tr) — renaming the domain requires renaming the `.pot` and all
`.po` files together or Babel silently produces empty catalogs.

## CI

`.github/workflows/tests.yml`: unit tests against a real SDK and libmpv, `bash -n` over every shell
script plus advisory shellcheck, a Docker build that then reruns the suite inside the image, a
prerelease zip per push, and an email report.

**Never interpolate `${{ github.event.* }}` into a `run:` block.** Commit messages are
attacker-controlled; pass them through `env:` and reference quoted shell variables. This was a live
script-injection sink until it was fixed. Issue titles and bodies are the same class of input —
`label-untemplated-issues.yml` uses `github-script` and never touches the text at all.

**The runner's mpv is older than the image's, and code that depends on the difference will pass locally
and fail in CI.** `ubuntu-latest` installs `libmpv2 0.37`; the image ships mpv 0.40. mpv 0.38 inserted an
`<index>` argument into `loadfile`, so the two want different argument counts, gated on
`MPV._LOADFILE_INDEX_API_VERSION`. The client API numbers are what matter and are easy to get wrong by
one release: **0.37 is (2, 2), 0.38 is (2, 3)**, 0.39 (2, 4), 0.40 (2, 5). A gate of (2, 2) captures 0.37
and breaks every load on the runner. Measure both sides rather than reasoning about it —
`test_mpv_loadfile.py` talks to a real libmpv and pins the boundary independently of the gate
expression, because the argument-order test uses the same condition as the code and so cannot catch a
wrong constant.

Labels named by an issue form must exist in the repository, or the form files the issue without them and
says nothing.

## Commit and phase conventions

Work proceeds in numbered phases from the plan; the plan's delivery table records what each phase
actually found, including where reality contradicted it. When a phase finishes, update that table —
future instances rely on it to know what is real versus planned.

### Every coding task publishes an auto-updating artifact page — where there is a browser to open it

**Where this applies.** The artifact exists to be opened in a browser, so it is only worth publishing
where one exists:

- **Windows host: publish.** Every coding task, as described below.
- **Linux with a desktop session: publish.** A desktop means a display server is running
  (`DISPLAY` or `WAYLAND_DISPLAY` is set, or `loginctl show-session` reports a type of `x11` or
  `wayland`).
- **Linux headless, command line only: do not create the page.** There is no browser on the machine,
  so the link cannot be opened there and the page is wasted effort. Do not publish an artifact, and do
  not offer to. Report in the terminal instead, keeping it short and structured: what is being
  changed and why, what changed, what is unverified and why, and a closing summary as the last thing
  printed. Everything else in this section that is not about the page itself still applies:
  `CHANGELOG.md` and the plan are updated in the same commit, and the unverified parts are named.
- **macOS and anything else:** treat as a desktop unless it is a remote shell with no display.
- **Unsure which it is:** check the environment variables above; if still unclear, ask once.

The rules below describe the page for the cases where it is published.

**Where it applies, it is not optional, and not only at the end.** Terminal scrollback is the worst possible medium for the
person this project is built for: it cannot be navigated by heading, a long tool result buries the one
sentence that matters, and re-reading it means arrowing through hundreds of lines that were only ever
meant for the machine. Publish the work as an Artifact instead, and hand back the link.

- **Publish early, then keep republishing to the same URL.** The page goes up as soon as there is
  something to say — what is being changed and why — and is republished as the work moves, so it is a
  live view rather than a report written afterwards. Pass the artifact's `url` to update it in place;
  a new URL is a new page and loses whatever the person had open.
- **Structure it for a screen reader, because that is the point of publishing it.** Real headings in
  order, a `<main>`, tables with a `<caption>` and `<th scope>`, state given in words and not only in
  colour, and a skip link. This is the same standard the web portal is held to; see "Accessibility is
  binding". Route the page through the `accessibility-lead` agent before publishing it, exactly as the
  hooks require for the portal.
- **Say what changed, what it replaces, and what is still unverified.** A status page that only lists
  green ticks is worth nothing. Name the parts that were not run, and why — no Docker on the host, no
  live account to test against, a test that exercises a stand-in rather than the real dependency.
- **End with a summary section.** Last on the page, so the review cursor lands on it, and written so it
  stands on its own for someone who reads nothing else.
- **The page is a view of the work, never the record of it.** `CHANGELOG.md` and the plan remain the
  source of truth and are still updated in the same commit. The artifact is not committed.

### First run in a fresh clone

If `~/.claude/plans/` has no copy of this project's plan, **seed it before doing anything else** —
before reading code, before making any change:

```bash
bash tools/install-hooks.sh
```

That installs the hooks and copies the repository's plan into `~/.claude/plans/`, without overwriting
one that is already there. Then read [.claude/plans/streamerbot-plan.md](.claude/plans/streamerbot-plan.md).

Do not skip this on the grounds that the plan is also readable in the repository. Every rule below about
comparing the two copies does nothing when one of them does not exist: the pre-commit hook finds no
counterpart and skips its drift check silently. Starting work without the plan means starting without
knowing which phases are done, what each one actually found, or which of its original decisions have
since been overturned — and several have.

### Every push adds a numbered CHANGELOG entry

In the same commit as the work, not at release time when nobody remembers what changed or why.

Entries go under `## Unreleased` at the top of [CHANGELOG.md](CHANGELOG.md), newest first, each opening
with a number in square brackets:

```markdown
## Unreleased

- **[003]** Connection and login failures now name the host, port and attempt number. They were
  logged already, but as "Connection failed" with no detail, which told a user nothing.
- **[002]** Unhandled exceptions in any thread now reach the bot's log rather than stderr.
```

- **Numbers only go up and are never reused**, including for a reverted change — the revert gets its
  own number and says what it undid. A number has to mean one thing forever to be citable in an issue.
- **One entry per change, not per commit.** A fix spread over three commits is one entry.
- **Say what it does and why.** "Fixed thread.py" is worthless in six months; the reasoning is the part
  the diff does not carry.
- On a release, `## Unreleased` becomes that version's section and a fresh empty one is started.
  **Numbering continues across releases** so a number identifies a change on its own.

### Commit messages are short; the CHANGELOG is where detail goes

A commit message is read in `git log`, usually several at a time, by someone scanning for the change
they are after. One that fills the screen buries the nine around it. The long version — the measurements,
the theory that turned out to be wrong, the reasoning the diff does not carry — belongs in the CHANGELOG
entry, which is numbered, citable in an issue, and read deliberately rather than scrolled past.

- **A subject line, then one or two short paragraphs.** The subject says what changed, in the imperative
  and under about 72 characters. The body says what was wrong and why the fix is the shape it is. If a
  third paragraph is starting, it is CHANGELOG material.
- **Do not hard-wrap inside a sentence.** Write each paragraph as one line and let the reader's terminal
  or viewer wrap it. A sentence broken across three hard newlines reflows badly everywhere it is
  displayed and is tedious to edit afterwards. Break between paragraphs, never mid-thought.
- **Do not restate the diff.** What changed is already in the patch; the message is for what the patch
  cannot say.
- **Point at the number.** When the change has a CHANGELOG entry, naming it — "see [052]" — lets the
  short message stay short and still lead anywhere.
- **Explain *why*, particularly where a choice looks odd.** Several non-obvious decisions in this
  codebase exist because the obvious version was tried and broke something. That is exactly the part
  worth the sentence, even in a two-paragraph message.

### On a host running the updater, an unpushed commit lives about five minutes

`streamerbot-updater.service` runs `auto_updater.sh`, which wakes every `STREAMERBOT_UPDATE_INTERVAL`
seconds — 300 in `project.env` — and invokes `AUTO_UPDATE=true update.sh` (`auto_updater.sh:197`). In
that mode `update.sh` answers its own confirmation prompt without asking anyone (`update.sh:525`) and
runs `git reset --hard "origin/$BRANCH"` followed by `git clean -fd` (`update.sh:567`). A commit that
exists only locally is discarded, uncommitted edits with it, and `git clean -fd` removes new untracked
files as well. **`git commit` and `git push` are one step on these hosts, not two.**

**The guard that looks like it prevents this does not.** `update.sh:433` prints "Local version has
diverged or is ahead of remote. Auto-pull skipped to protect local changes" and does skip `NEEDS_PULL` —
but the reset is reached through `NEEDS_REBUILD`, which a local commit *guarantees*: `NEEDS_REBUILD` is
set whenever `LOCAL_HASH != RUNNING_HASH` (`update.sh:445`), and `RUNNING_HASH` is the commit baked into
the running image's `commit_hash` label. Committing is itself what triggers the rebuild that throws the
commit away, and the reassuring message is printed on the way there. A branch is no refuge either: the
reset targets `origin/$BRANCH` with `BRANCH` defaulting to `main` (`update.sh:381`), so it is whatever
the updater is tracking, not whatever you are on.

Two things follow, and the first one has already cost a session:

- **`git push` reporting "Everything up-to-date" moments after you committed means the reset has already
  happened.** The commit is not lost yet — `git reflog` still names it, and `git merge --ff-only <sha>`
  puts it back on the branch. Push it straight away. Read literally the message is true, which is what
  makes it so easy to take for "done".
- **Work that must sit uncommitted needs the timer stopped** (`systemctl stop
  streamerbot-updater.service`) or needs to live outside the repository. Nothing inside it survives a
  `reset --hard` plus `clean -fd` on a five-minute loop.

### Keeping the plan and this file in sync

**This repository is the source of truth, and the sync runs both ways.** The plan is authored at
`~/.claude/plans/` and lives here at `.claude/plans/streamerbot-plan.md`. The repository has
collaborators with write access, so either copy can move first — a phase finished locally advances one,
a merged PR advances the other.

A pre-commit hook enforces this. Install it once per clone:

```bash
bash tools/install-hooks.sh
```

It refuses a commit whose plan has drifted from the local copy, and refuses any
commit containing private `.claude` files (the OAuth credentials, session transcripts). It finds the
local plan by matching the document's first heading, not its filename, and does nothing on a machine
with no local plan, so collaborators are unaffected. `git commit --no-verify` bypasses it. The private
file check is repeated in CI, where it cannot be bypassed.

**Check GitHub before writing a single word.** This is a precondition, not a step to remember later. No
edit to the plan or to this file begins until the repository's copy has been fetched and compared:

```bash
git fetch origin main
git log --oneline HEAD..origin/main -- .claude/plans/streamerbot-plan.md CLAUDE.md
diff ~/.claude/plans/*.md .claude/plans/streamerbot-plan.md
```

Anything the second command lists is someone else's writing that you do not have. Pull it and build on
it. Checking first is the only point at which reconciling is cheap — edit first and both documents have
moved, and merging two prose revisions of the same paragraph is manual work that gets resolved by
picking a side, which is how a collaborator's contribution disappears without anyone noticing.

- **Repository moved, local did not** — take the repository's version as the base. Someone recorded
  something a phase found, or corrected a decision. Working from a stale plan is how the same phase gets
  implemented twice, differently, and it is worse than no plan because it still reads as authoritative.
- **Local moved, repository did not** — copy across and push in the same commit as the work.
- **Both moved** — **merge by hand, never `cp`.** A copy in either direction silently discards whatever
  the other side wrote, and because the plan is one long prose document, nothing fails loudly afterwards
  to reveal it. `git log -- .claude/plans/streamerbot-plan.md` names who changed it and why.

A phase is not finished until the plan recording what it found is pushed. Treat the pushed version as
what the project has agreed; treat anything only in the local copy as not yet real.
