# Changelog

Every change gets a number in square brackets. Numbers only ever go up and are
never reused, so `[004]` refers to one change permanently and can be cited in an
issue or a commit message. Numbering continues across releases.

## Unreleased

- **[013]** Every bot gets its own account portal and Spotify port, which fixes
  Apple Music sign-in and Spotify on every bot but the first. `li am` reported
  "The account portal is switched off in this bot's configuration" on a bot whose
  configuration said it was on. It was not a configuration problem: bot
  containers run with `--network host`, and `auth_portal.port` (4419) and
  `services.sp.api_port` (3678) were written into every bot as the same
  constants. The first bot to start took both, and the rest failed — the portal
  with `Address already in use`, go-librespot by exiting a second after every
  start, forever. Both failures were invisible: a portal that failed to bind was
  indistinguishable from one switched off, so the bot blamed the config and sent
  people to check a correct file, and go-librespot's output went to `/dev/null`
  so its restart loop logged no exit code and no reason.

  Ports are now allocated per bot on creation, on restore, and automatically
  before Start All and Restart All, so bots already clashing are repaired without
  anyone knowing to ask. There is also `Manage Bots` → `Repair Account Portal and
  Spotify Ports` and a `--repair-ports` flag. Allocation is sticky and
  idempotent: a bot keeps the ports it has, and a bot's own live listener is not
  treated as a clash — counting it would move the port on every restart and break
  the firewall rule and `public_url` set up for it.

  When no port can be found, the portal is switched off deliberately rather than
  left pointing at a port it cannot have, and the bot's folder gets a
  `PORT_CONFLICT.txt` naming the recovery path. It also covers the case where the
  port is free but a firewall or a router is not letting it through to the host,
  because that produces the same symptom for a different reason. Repairing turns
  the portal back on.

  `li` now distinguishes "switched off in the configuration" from "failed to
  start", and the bind failure names the port and the fix. go-librespot's stderr
  goes to `go-librespot.log` in the bot's own directory at 0600 — the reason
  `/dev/null` was chosen was to keep its credentials blob out of the shipped log,
  and that directory is already 0700 and holds `credentials.json`. Its exit code
  is logged, and a daemon that will not stay running says so once at ERROR
  instead of only repeating a warning that reads like a single restart.

- **[012]** YouTube plays again. Searching worked, and every result and every
  pasted YouTube link then failed — "The selected service is currently
  unavailable" for a search, "Cannot process stream URL" for a link — while
  Icecast and other direct streams were fine, because those need no resolving.
  Three bugs stacked up in the bridge's client fallback chain, which read
  `['YTMUSIC', 'MWEB', ClientType.TV_EMBEDDED]`:

  - The caller rewrote `'YTMUSIC'` to MWEB, so the first two entries sent an
    identical request and came back with an identical error.
  - `ClientType.TV_EMBEDDED` is the enum **value**
    `'TVHTML5_SIMPLY_EMBEDDED_PLAYER'`, but youtubei.js validates the `client`
    option against the enum **keys**, so that entry was rejected inside the
    process and had never once worked. `ClientType` is the vocabulary for
    `Innertube.create({client_type})` and the wrong one for `getBasicInfo`.
  - So one real client was ever tried — and YouTube answers 400 to an
    OAuth-authenticated player request that it serves anonymously. Signing in to
    reach age-restricted content therefore broke ordinary playback, with nothing
    behind it. The bot worked until the moment someone completed `yl`.

  The chain is now distinct, valid clients, and a signed-in bot falls back to an
  anonymous session so an account YouTube dislikes can no longer take playback
  down. Signed-in attempts still come first, because they are the only ones that
  can return age-restricted or member content. Verified against the two videos
  from the reported log: one resolved on MWEB, the other only on IOS — a client
  the old chain did not contain, so it had no route to a stream at all.

  The chain moved to `media.mjs` as a pure function, because the ordering is
  what was wrong and it was not reachable from a test where it was.

- **[011]** `loadfile`'s version gate was one mpv release too low, which broke
  playback on every mpv older than 0.40 and turned CI red. [008] correctly found
  that mpv 0.38 inserted an `<index>` argument into `loadfile` and started sending
  it, but gated that on client API `>= (2, 2)`. `(2, 2)` is mpv **0.37**, the last
  release that wants the old form — 0.38 reports `(2, 3)`. So 0.37 was handed an
  argument it rejects, and every load raised `MPV_ERROR_INVALID_PARAMETER`.

  The gate is now `(2, 3)`. Both sides were measured rather than reasoned about,
  since guessing wrong in either direction breaks all playback on one of them:
  0.37 accepts `replace start=0` and rejects `replace -1 start=0`; 0.40 does the
  exact reverse. Ubuntu 24.04 ships 0.37 and the container ships 0.40, which is
  why this passed locally and failed on every GitHub Actions run.

  The existing argument-order test could not have caught it: its condition is the
  same expression the code under test uses, so it passes for any value of the
  constant. Three tests now pin the boundary itself, and the note on that test
  says what it does and does not cover.

- **[010]** The portal link uses an address a user can actually open. It handed
  out `http://127.0.0.1:4419`, which on the remote Linux box these bots run on is
  a link to the user's own computer, where nothing is listening — so every attempt
  to connect an account died with a browser error and no explanation. The address
  now comes from `auth_portal.public_url` if set, then
  `STREAMERBOT_PUBLIC_URL`, then the address of the interface that routes to the
  internet, which on a VPS is the public IP. A third-party lookup service is
  deliberately not used: it would answer better behind NAT, but it tells someone
  else's server where the bot lives every time the portal starts.

  Detecting the address is only half of it, so `li` and `ap link` now also say
  what will stop the link working. The portal still binds to loopback by default,
  because it takes account passwords, so the advice names the exact change:
  set `auth_portal.host` to `0.0.0.0`, restart, and open the port. Bot containers
  use host networking, so no Docker port mapping is involved.

- **[009]** Configurations are checked before any bot is started, and a
  configuration from another fork is no longer taken at its word. Creating a bot
  now scans every bot on the host, not just the new one, and says which ones will
  not connect and why; restoring does the same after migrating. The check runs
  inside the image and asks `bot/config/inspection.py`, beside the model and the
  migration table that define the rules, rather than restating those rules in
  `jq` — two copies of the same rules drifting apart is what `[007]` was. It
  mounts `bots/` read-only, so it is safe to run while every bot is up.

  A server on the same box is not a complaint. Containers are created with
  `--network host`, so `localhost` inside a container is the host, and running
  the TeamTalk server alongside the bots is an ordinary way to do this. What is
  worth saying is the shipped template nobody filled in — server, account and
  nickname all still at their defaults — which is a different thing and is
  reported as such.

  The stricter part is lineage. Version numbers only mean something within one
  project: a config restored or copied from `gumerov-amir/TTMediaBot`, from
  `JoaoDEVWHADS/TTMediaBot` or from any other fork can declare a `config_version`
  this bot also uses and mean something entirely different by it. Migration used
  to return early on a matching number, so such a file passed straight through —
  keeping TTMediaBot's cache and log filenames and, worse, a `default_service` of
  `vk`, which `ServiceManager` looks up in a plain dict and dies on. That was a
  `KeyError` at startup, before TeamTalk, with a traceback instead of an
  explanation. Lineage is now decided by the shape of the file, never by its
  version number, and such a config is migrated whatever number it carries; a
  service name that has no equivalent here falls back to the default and says so.
  Nothing touches the nickname, the server or the channel.
- **[008]** Playing anything works again. mpv 0.38 inserted an `<index>`
  argument into its `loadfile` command, between the flags and the per-file
  options, and the vendored `mpv.py` still passed its options string in the old
  third position. Debian 13 ships mpv 0.40, so against the image's own libmpv
  every load was rejected with `MPV_ERROR_INVALID_PARAMETER` and the user got a
  private message reading "Invalid value for mpv parameter" instead of audio.
  `loadfile` now sends the two-argument form when there are no per-file options,
  which every mpv release accepts and which is the only form the bot itself uses,
  and inserts the index ahead of the options when there are. Tests cover both the
  arguments assembled and a real load through libmpv, because the argument test
  alone only restates the assumption that was wrong.
- **[007]** A newly created bot starts instead of exiting at once. `config.json`
  declared `config_version` 2 while `ConfigManager` still understood 1, and a
  version above what the bot knows is rejected outright — so every bot created
  since that change died during configuration loading, before it reached
  TeamTalk, and `--restart always` then restarted it forever. To a user the bot
  was running and simply never connected. The version bump on the shell side now
  has a matching migration on the Python side, the rejection message names both
  version numbers instead of saying only "invalid config_version value", and a
  configuration written before versioning existed is migrated rather than
  silently left alone. Tests now read the migrator, which nothing did before,
  including one that fails if the two version numbers drift apart again.
- **[006]** The bot's log records account sign-in outcomes. Every state change
  logged at debug before, and the default level is INFO, so a failed sign-in
  produced no log line at all — the one event someone most needs when asking why
  an account will not connect. Failures now log at error, CAPTCHAs at warning,
  and a code request that times out says how long it waited.
- **[005]** Connection and login failures to the TeamTalk server name the host,
  the port, the attempt number and the retry limit. They were already logged, but
  as "Connection failed" and "Login failed" with no detail, which told a reader
  nothing they could act on. The password is deliberately not among the details.
- **[004]** Unhandled exceptions in any thread reach the bot's log instead of
  stderr, which a container sends nowhere useful. This matters because the bot
  runs seventeen threads — the mpv event thread, the browser worker, the librespot
  monitor, one per command — and a thread dying silently is how "playback just
  stops" becomes unreportable. Tracebacks go through logging, so the secret
  redaction filter scrubs them: a traceback can carry a password in a local
  variable.
- **[003]** Creating a bot writes a log at `logs/manager.log`, with the steps it
  ran and what they returned. `docker create` sent its output to `/dev/null`, so a
  failed creation said only "Error creating" with no reason. The log sits outside
  `bots/` because a creation that fails early never gets a bot directory, which is
  exactly the case worth having a record of. Passwords are not written to it.
- **[002]** No `cookies.txt` is ever written into a bot's folder, copied when a
  bot is duplicated, or mounted into a container. Nothing has read one since
  sign-in moved to device codes, so it was a stale credential in plaintext.
- **[001]** Restoring a backup from the old TTMediaBot deletes its obsolete
  `cookies.txt` during migration, before any container starts. This reverses an
  earlier decision to keep it and merely report it: on reflection a dead YouTube
  session sitting in a directory that gets tarred into every backup is worse than
  the small loss of deleting a file nothing reads.

## StreamerBot 1.0 (unreleased)

A rewrite of TTMediaBot. The multi-bot Docker architecture is unchanged; almost
everything else moved.

### Six services instead of two

YouTube and YouTube Music are joined by **Spotify, Netflix, Disney+, Apple Music
and Amazon Music**. They do not all work the same way, and the differences are
worth knowing before choosing one:

| Service | Plays through | Needs |
| --- | --- | --- |
| YouTube, YouTube Music | mpv | Nothing. Signing in only adds age-restricted content |
| Spotify | go-librespot | Premium. A Spotify application for search by name |
| Netflix, Disney+ | Chrome | An account. **amd64 only** |
| Apple Music, Amazon Music | Chrome | An account. **amd64 only** |

**On ARM machines, including Raspberry Pi, the four browser services do not
work.** Google publishes no Chrome for linux/arm64 and only Chrome carries the
Widevine module those services need to decrypt anything. Those services disable
themselves and say so, rather than failing when someone asks for a title.
YouTube, Spotify and direct URLs work everywhere.

### No more cookies.txt

YouTube used to need a `cookies.txt` exported from a desktop browser every few
weeks. It now signs in with a device code: the bot reads out a short code, you
enter it at `google.com/device`, and the tokens refresh themselves from then on.
Spotify pairs the same way at `spotify.com/pair`.

Existing bots keep working without signing in at all — public videos never needed
an account.

### Accounts connect through a web portal

Netflix, Disney+, Apple Music and Amazon Music need a password, which does not
belong in a channel where everyone can read it. `li` gives a private link to a
portal page. Two-factor codes have their own step, so a code prompt no longer
dead-ends the sign-in.

Credentials are encrypted at rest with a per-bot key and are never written to a
log. **Each bot's accounts are entirely its own** — two bots on one machine never
share a login.

Read the honest limits in the README: the key sits beside the ciphertext, so this
protects backups and casual disclosure, not an attacker with root on the host.

### Audio description

Films and shows can play with the described audio track where the service offers
one. The bot asks first and can remember the answer: `da on`, `da off`, or `da`
to be asked each time. An unanswered prompt falls back to the default after
thirty seconds rather than leaving playback waiting.

### Apple Music downloads

`dl` on an Apple Music link downloads it and uploads it to the channel. A single
track arrives as one file; an album, artist or playlist arrives as one zip,
because forty separate uploads is forty separate announcements.

### New commands

`li` connect an account or see what is connected · `da` audio description ·
`pf` streaming profiles · `yl` YouTube sign-in · `ap` the auth portal ·
`lo` disconnect a service and delete its saved sign-in.

### The manager is easier to use without sight

`streamerbot.sh` no longer clears the screen, because scrollback is how a screen
reader user reviews what just happened. Progress is printed as discrete steps
rather than a self-rewriting line, which is read aloud continuously. Destructive
actions ask for a typed word instead of `y/N`, since capitalisation conveys
nothing when spoken. Status lines start with the word `OK`, `Warning` or `Error`
rather than relying on colour.

**Uninstall now asks what "everything" means.** The safest level removes the
containers and the image and leaves every account and all configuration intact,
so rebuilding brings the bots back as they were.

**Flags for people who would rather skip menus:** `--status`, `--services`,
`--start-all`, `--stop-all`, `--restart-all`, `--check-updates`, `--logs NAME`,
`--help`.

### Under the hood

- **Debian 13**, Python 3.13, pydantic v2.
- **TeamTalk SDK 5.22a**, with the official Python binding. The vendored copy was
  ABI-incompatible: eleven structs had the wrong size and the event union was
  missing a member, so on 5.22 every event would have been misparsed.
- `Player` became a transport over playback engines, because Spotify and Chrome
  produce audio directly into the sink and there is no URL to hand mpv.
- Update checks run **hourly** rather than every twenty seconds, and opening the
  manager no longer runs a full update — it says whether one is available.
- Sound devices can be chosen by name rather than by index, which shifts whenever
  the set of devices changes.
- Backups exclude browser caches and logs, so they stay small enough that people
  keep taking them.

### A note on terms of service

Rebroadcasting Netflix, Disney+, Apple Music or Amazon Music into a TeamTalk
channel is very likely a breach of those services' terms. That is the operator's
decision, and the README says so in its first paragraph.
