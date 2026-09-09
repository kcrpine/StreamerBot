# StreamerBot — multi-service accessible TeamTalk streaming bot

## Context

`C:\Users\kcrpi\Documents\teamtalk tv streamer and music bot\TTMediaBot` is a fork of TTMediaBot
(origin `JoaoDEVWHADS/TTMediaBot`). Today it streams **YouTube and YouTube Music only**, authenticated
by a Netscape `cookies.txt` file that the operator must manually re-export from a browser every few
weeks. It runs one Docker container per bot, managed by `ttbotdocker.sh`, on `python:3.10-slim-bullseye`
with TeamTalk SDK 5.8.1.

We are turning it into **StreamerBot**: same multi-bot Docker architecture, but

- **no more cookie files** — YouTube signs in with an OAuth device code that refreshes itself forever;
- **six services** — YouTube, Spotify, Apple Music, Amazon Music, Netflix, Disney+, plus direct URLs;
- **users connect their own accounts** through an accessible web portal and chat prompts;
- **audio description** can be switched on for any movie or show, with the bot asking first;
- Debian 13, TeamTalk SDK 5.22, and auto-update pointed at the user's own GitHub fork.

Primary users are blind. Every surface — the web portal, the TeamTalk chat prompts, and the bash
manager — is designed against WCAG 2.2 AA and screen-reader-first CLI conventions.

### Settled during implementation

- GitHub repo is **`kcrpine/StreamerBot`**, baked into `project.env`.
- TeamTalk SDK URLs verified live against bearware.dk: `tt5sdk_v5.22a_ubuntu22_x86_64.7z` and
  `tt5sdk_v5.22a_raspbian_arm64.7z` (both return 200; the ARM build is `raspbian_arm64`, and the
  filenames are lowercase — my first guesses at both were wrong).
- The in-container launcher `TTMediaBot.sh` became **`run_bot.sh`**, not `StreamerBot.sh`: Windows is
  case-insensitive, so `StreamerBot.sh` and the `streamerbot.sh` manager would be the same path and
  cannot coexist in the working tree.

### Discovered during Phase 0 — the binding-drift risk was real

The vendored `TeamTalkPy/TeamTalk5.py` was **ABI-incompatible with SDK 5.22a**, exactly the silent-memory-
corruption risk the plan flagged. Measured against the official 5.22a binding loaded on the same DLL:
11 structs differed in size (`TextMessage` 2064 vs 2068, `Channel` 5280 vs 5288, `UserAccount` 4184 vs
6232, `RemoteFile` 2064 vs 3088, `BannedUser` 5124 vs 6148, …) and `TTMessageUnion` was missing its
`sounddevice` member entirely — 19 fields against 20. Since `getMessage()` reads every event through that
union, the old binding on a 5.22 library would have misparsed all of them.

Fixed by replacing the vendored file with the SDK's own `Library/TeamTalkPy/TeamTalk5.py`, which exposes
every name the bot uses (16 symbols, all present) and all 21 called DLL exports. One local delta: upstream's
Windows-only `os.add_dll_directory(... "/../TeamTalk_DLL")` is guarded with `isdir()`, because it raises
`FileNotFoundError` and makes the module unimportable when that directory is absent.

### Environment notes

- **Disk**: the C: drive was at 18 MB free when Phase 0 started, which broke WSL with I/O errors mid-build.
  The Ubuntu-24.04 WSL distro was moved to `D:\wsl\Ubuntu-24.04` (`wsl --manage --move`), returning C: to
  ~5 GB free and giving Docker room on D:'s 167 GB. Docker Engine runs inside WSL; Docker Desktop was not
  needed.
- **Testing**: the SDK and libmpv were downloaded to verify locally and then deleted, so the suite no longer
  runs on the Windows host — run it in the container instead.

### Added after the plan was approved

- **Auto-update checks hourly, not per-push.** `STREAMERBOT_UPDATE_INTERVAL=3600` in `project.env`
  replaces `auto_updater.sh`'s 20-second poll. The updater wakes on that interval and only then asks
  GitHub whether the branch moved; it does not react to individual pushes. `auto_updater.sh` clamps the
  value to a 300-second floor.
- **Legacy backup migration.** `streamerbot.sh` gains "Migrate a backup from the old TTMediaBot" which
  takes an old `backup_bots_*.tar.gz`, and per bot directory: renames `TTMediaBotCache.dat` →
  `StreamerBotCache.dat` and `TTMediaBot.log` → `StreamerBot.log`, runs the config through the v2
  migrator (adding `services.spotify`/`netflix`/`disney`/`apple_music`/`amazon_music`/`audio_description`
  and `auth_portal` with defaults while preserving TeamTalk credentials, channel and nickname), creates
  `secrets/`, `browser/`, `youtube_auth/`, `librespot/`, retains the old `cookies.txt` as an importable
  session but reports that YouTube now needs a one-time device-code sign-in, `chown -R 1000:1000`, and
  recreates the containers on the new image. Dry-run first, printing what it will do per bot.
  The existing backup and restore functions are kept as they are.

### Added during Phase 3

**A prerelease per push, with email.** GitHub Actions builds a source zip on every push, publishes it
as a prerelease tagged `build-<run number>-<short sha>`, and records in the release notes whether the
checks passed. Two reasons this earns its place: every commit gets a downloadable artifact, and
publishing a release is the **only native GitHub mechanism that emails on a push** — the old Email
service webhook was removed in 2019, and watching a repository does not notify on raw commits. The CI
notify job also carries the build tag and link in its own email. A release per push accumulates fast,
so the job prunes to the most recent 20 prereleases; real tagged releases are untouched.

**A gamdl wrapper for Apple Music downloads.** Separate from the Apple Music *playback* engine in
Phase 6 — this is downloading, not streaming, and the two share nothing but the service name.

- New `bot/services/gamdl.py` wrapping the `gamdl` CLI, invoked through `subprocess` with an argument
  list, never a shell string, since track and album names arrive from user input.
- Output is transcoded to **MP3** and uploaded to the channel the bot is currently in, reusing the
  existing `download` and file-upload plumbing in `Command` rather than inventing a second path.
- A single track uploads as one MP3. **An album, artist or playlist is zipped first** and uploaded as
  one archive, because uploading forty files individually into a TeamTalk channel is unusable with a
  screen reader — each arrival is its own announcement.
- Reuses the existing `delete_uploaded_files_after` config so downloads do not accumulate on disk, and
  the existing `pending_search_results` selection flow so `sl N` picks what to download.
- Progress is reported as **one message when the download starts and one when the upload finishes**,
  never a percentage that rewrites itself: the TeamTalk chat announcement rules in this plan exist
  because a self-updating counter is read aloud in full on every tick.
- Needs Apple Music credentials, so it goes through the same per-bot `SecretStore` and the auth portal
  as the playback side, and is disabled with a spoken reason when the account is not connected.
- gamdl requires a widevine device file for anything above 256kbps AAC; when it is absent the wrapper
  falls back rather than failing, and says which quality it got.

**Ship the Claude working structure with the repo.** `.claude/` is committed so a contributor using
Claude Code starts from the same plan and the same rules: `plans/streamerbot-plan.md` (this document),
the three accessibility hooks, a `settings.json` wiring them through `$CLAUDE_PROJECT_DIR` so it works
from any clone, and a README explaining the setup.

**A `CLAUDE.md` at the repository root, kept current.** Written for the next Claude Code instance
rather than for a human reader, so it carries the things that are expensive to rediscover: that the
test suite cannot run on a Windows host because the SDK and libmpv binaries are not in the repo and
must be run in the container; that `project.env` is the only place a repo URL, image name or SDK
version may live, and that its keys contain digits so a `[A-Z_]+` filter silently drops them; that
`Player` is a transport over engines and engines must never touch `player.state`; that `bot_id` is the
containment boundary in the shared bridge and its regex is load-bearing; that credentials live only
under a bot's own `data/`; and the specific accessibility rules that are easy to regress and are pinned
by tests.

It also names the traps that already cost time once: the `:rw` mount, the bridge's process-level
rejection handlers, `update.sh` pruning `bots/` from its permission pass, and never interpolating
`${{ github.event.* }}` into a `run:` block. **This file is updated as part of finishing a phase**, in
the same commit as the plan's delivery table, because a stale CLAUDE.md is worse than none — it is
confidently wrong.

**The repository is the source of truth for the plan and `CLAUDE.md`, and the sync runs both ways.**
The plan is authored at `~/.claude/plans/` and lives in the repository at
`.claude/plans/streamerbot-plan.md`. With collaborators holding write access, either copy can move
first: a phase finished locally advances the local one, and someone else's merged PR advances the
repository's. Whichever moved, the two have to be reconciled before any further work, because both
directions of a blind copy destroy somebody's writing.

**On a fresh clone, seed the local copy before doing anything else.** Someone who has just cloned this
repository has no plan in `~/.claude/plans/` at all, so every rule below about comparing the two copies
silently does nothing: the pre-commit hook finds no counterpart and skips its drift check, and nothing
else notices either. The failure is quiet and the consequence is not — they start work with no idea
which phases are done, what each one actually found, or which of the plan's original decisions have
since been overturned.

So the first action in a fresh clone, before reading code and before touching anything, is to take the
repository's copies:

```
cp .claude/plans/streamerbot-plan.md ~/.claude/plans/
```

and read `CLAUDE.md` at the root. `tools/install-hooks.sh` does the copy as part of setting the hooks
up, so running it once covers this too, and it will not overwrite a local plan that already exists.

This is why the plan carries a running record of what each phase found rather than only what each phase
intends. A contributor arriving at Phase 6 needs to know that the vendored TeamTalk binding was ABI
incompatible, that go-librespot's device auth flow does not exist before 0.9.1, and that five of the
portal's "settled" accessibility decisions were wrong — none of which is recoverable from the code, and
all of which would otherwise be rediscovered the expensive way.

**Check GitHub before writing a single word.** This is a precondition, not a step in a checklist: no
edit to the plan or to `CLAUDE.md` begins until the repository's copy has been fetched and compared.
Not at the end, not before committing — before typing.

```
git fetch origin main
git log --oneline HEAD..origin/main -- .claude/plans/streamerbot-plan.md CLAUDE.md
```

Anything listed there is someone else's writing that the local copy does not have. Pull it and take
that version as the base, then carry the local edits onto it.

The reason for the ordering is that it is the only point at which reconciling is cheap. Edit first and
the two documents have both moved, and merging two prose revisions of the same paragraph is manual,
error-prone work that a tired person resolves by picking one side — which is exactly how a
collaborator's contribution disappears without anyone noticing. Check first and the common case is that
nothing has changed and the cost is one command.

Working from a stale plan is also how two people implement the same phase differently, each believing
they are following the agreed design. That is worse than having no plan at all, because a stale plan
still reads as authoritative.

**Push in the same commit as the work.** Any change — a phase marked done, a decision overturned, a new
requirement — goes up alongside the code it describes. A phase is not finished until the plan recording
what it found is pushed.

**When both have changed, merge, never overwrite.** Diff the two and reconcile by hand. `cp` in either
direction silently discards whatever the other side wrote, and because the plan is one long prose
document rather than code, nothing will fail loudly afterwards to reveal that it happened. If the
repository's copy has moved and its history is unclear, `git log -- .claude/plans/streamerbot-plan.md`
names who changed it and why.

**A pre-commit hook enforces both halves of this**, because a convention nobody can forget is worth
more than one everybody agrees with. `.githooks/pre-commit`, installed per clone by
`tools/install-hooks.sh`, refuses a commit whose plan has drifted from the local copy, and refuses any
commit carrying private `.claude` files. It finds the local plan by matching the document's first
heading rather than its filename, since the authored copy is named after the session that created it,
and it does nothing at all on a machine with no local plan, so a collaborator cloning the repository is
unaffected. `git commit --no-verify` bypasses it deliberately.

Hooks live in `.githooks` rather than `.git/hooks` because the latter is not versioned and does not
survive a clone; `core.hooksPath` is set by the installer, which git requires be run by hand, since a
repository that could install its own hooks could run arbitrary code on checkout. The private-file half
is therefore repeated in CI, where it cannot be skipped and does not depend on anyone having run the
installer.

The practical consequence: the local copy is a working copy, not the original. Treat the pushed version
as what the project has agreed, and treat anything only in the local copy as not yet real.

**Curated, not copied wholesale.** A `.claude` directory also contains `.credentials.json` — a live
OAuth access token and refresh token for the account — and `projects/`, the full transcript of every
session run on that machine. On a public repository that is a credential leak and a privacy leak
respectively, so both, along with `history.jsonl`, `cache/`, `sessions/`, `shell-snapshots/`, `debug/`
and the rest of the machine state, are excluded by name in `.gitignore`. The accessibility agents
themselves are referenced rather than vendored: they belong to their own project and are better
installed from source, and the hooks degrade to printing their reminder when the agents are absent.

### Search result ordering, for Spotify, Apple Music and Amazon Music

All three services return several kinds of thing for one query — tracks, albums, artists, playlists —
and each ranks them by its own relevance model. The bot must not flatten that into one undifferentiated
list, and must not impose an ordering of its own invention either.

**Find out what the service actually considers the best match, and lead with it.** Each service's search
API returns its own notion of top results: Apple Music has a `top` results type, Spotify returns
per-type result sets whose first entries carry its ranking, Amazon Music likewise. Query for the types
the service supports, keep the service's own ordering within each type, and present the strongest match
first rather than re-sorting by title, popularity or duration. The ordering is the service's answer to
the question; second-guessing it produces worse results and is unpredictable for the user.

**Then a numbered list, grouped by kind, artists and albums and playlists before individual tracks.**
The numbering is what `sl N` already selects on, so this reuses `pending_search_results` rather than
inventing a second selection mechanism. Grouping matters more here than in a visual UI: a sighted user
skims a mixed list and picks out the album; a screen reader user hears all twenty entries in order, so
"three albums, then five artists, then the tracks" is navigable where an interleaved list is not.

Announce it as a short summary first — how many of each kind — then the numbered entries, each stating
its kind: `1. Album: Abbey Road, The Beatles, 17 tracks`. Kind first in the line, because that is the
word the user is listening for, and it lets them stop reading once they hear the one they want. Keep
the count within the existing `search_results` config rather than a new limit.

Selecting an album, artist or playlist expands it into its tracks through the same service `get()` path
a pasted link uses, so there is one expansion code path per service and not two.

### Help must explain how to connect each service

`h` today prints one line per command and nothing else: with the new commands that is 58 lines in a
single message, which a screen reader reads start to finish with no way to skip. Worse, nowhere in it
does a user learn *how* to connect an account — only that `li` exists. The six services sign in three
different ways, and a user who has just been told "Spotify is not connected" has no route from there to
a working bot.

**Add help topics alongside the per-command help.** `h` keeps listing commands, but gains a short
header pointing at the topics, and `h connect` becomes the entry point:

- `h connect` — the three sign-in methods, which services use which, and what to type. Not a wall of
  text: name the method, name the services, give the command.
- `h connect <service>` — the full walkthrough for one service, which is where the detail lives.

**The three methods, because they genuinely differ:**

1. **Device code in chat** — YouTube and Spotify. `li yt` or `li sp`, the bot replies with a URL and a
   code, the user enters it on any device. Nothing is typed into the bot.
2. **The web portal** — Netflix, Disney+, Apple Music, Amazon Music. `li` gives a link; the account
   name and password are typed there, never in chat, and a 2FA step may follow.
3. **Operator setup, done once, not per user** — the Spotify application below. This is the one that
   confuses people, because it is not a sign-in at all.

**Spotify needs both, and help must say so plainly.** This is the case most likely to leave someone
stuck, because connecting the account correctly still leaves search broken, with no obvious link
between the two:

- **To play**: send `li sp`. The bot replies with a code and the address `spotify.com/pair`. Open that
  address on a phone or computer, sign in to Spotify if asked, enter the code, approve. The bot picks it
  up on its own; send `li` to confirm. Playback needs Spotify **Premium**.
- **To search by name**: the bot needs a Spotify application, which is the operator's job and is done
  once for the whole bot rather than per user. Go to `developer.spotify.com/dashboard`, sign in, choose
  Create app, give it any name and description, and for the redirect URI enter
  `http://localhost:4419/` since nothing will use it. Open the app's settings to find the **Client ID**
  and, behind "View client secret", the **Client Secret**. Put the client ID in the bot's configuration
  as `services.sp.client_id`, and give the secret to the bot with `lo`-style admin entry so it is stored
  encrypted rather than sitting in `config.json`.
- Say explicitly that **pasting a Spotify link works without the application** — only searching by name
  needs it. Otherwise a user who cannot search assumes their account pairing failed.

**Netflix and Disney+ walkthroughs**, since these are the ones with the most steps and the two extra
concepts nothing else has:

- **To connect**: send `li nf` (or `li dp`). The bot replies with a link that opens the account portal
  in a browser. The email address and password are typed **there, not in the channel**, because a
  message in a channel is visible to everyone in it. If the service texts or emails a code, the portal
  asks for it on its own page; paste or type the whole code into the single box and select Verify.
  Say plainly that a CAPTCHA cannot be answered by the bot, and what to do instead.
- **State the requirement honestly**: this needs Google Chrome, so it does not work on a Raspberry Pi
  or any other ARM machine. On those the service disables itself and says so rather than failing when
  someone tries to play something.

**Profiles**, which exist on Netflix and Disney+ and nowhere else. Explain what they actually change,
not just how to switch: each profile has its **own watchlist and its own audio and subtitle settings**,
so picking the wrong one means the bot cannot see the titles the user expects.

- `pf` on its own lists the profiles as a numbered list.
- `pf` and a number selects one.
- Mention that the choice persists until changed, so it is a once-per-bot thing rather than per play.

**Audio description**, which for this project's users is the point rather than a nicety. Help should
say what it *is* — a second audio track that narrates what is happening on screen — because a user who
has never had access to one may not know to ask for it.

- `da` on its own says what the current setting is.
- `da on` plays with description whenever a title has one, and stops asking.
- `da off` never uses it, and stops asking.
- `da ask` returns to being asked each time.
- Explain the prompt itself: when a title has a described track the bot asks, with four numbered
  options, and **answers after about thirty seconds by falling back to the default**, so a film never
  sits waiting on a question nobody answered. Say that not every title has one, and that the bot only
  asks when there is something to offer.

Read the codes out character by character in chat, the same as the device-code page does, and spell the
addresses as words rather than URLs where a synth would otherwise run them together.

Every string goes through `translate(...)`, and the walkthroughs are prose rather than tables, since a
table read linearly by a screen reader loses its column headings.

### Uninstall must ask what "everything" means

`streamerbot.sh`'s menu item reads "Uninstall Everything (Total Cleanup)", which is both frightening and
inaccurate: it delegates to `uninstall.sh`, which already offers a safe path. Nobody reading the menu
knows that, so the entry point has to ask the question in plain words instead of hiding three very
different outcomes behind one label.

**Three levels, asked as one question, with the least destructive first and selected by default:**

1. **Just the bots' containers and images.** Stop and remove every container labelled
   `role=streamerbot` plus the shared YouTube service, and remove the `streamerbot` image. **Leave
   `bots/` alone.** This is the option someone wants when they are reclaiming disk space or forcing a
   clean rebuild, and it must not touch a single credential: every bot's YouTube tokens, encrypted
   service passwords, Chrome profiles and configuration survive, so `streamerbot.sh` afterwards
   rebuilds and the bots come back as they were.
2. **The bots' containers, images and all bot data.** As above, plus `bots/`, the systemd updater unit
   and the temporary files. This is the current "Standard Uninstall". Say explicitly that this deletes
   every connected account and cannot be undone, and that a backup can be taken first.
3. **Docker itself as well.** Prune the whole Docker system and stop the engine. Say plainly that this
   affects **containers that have nothing to do with StreamerBot**, since a VPS commonly runs other
   things, and that it is the only option here that can break unrelated software.

The wording matters more than the mechanism. "Do you also want to remove Docker itself? Answering no
removes only StreamerBot's own containers and images" is answerable; "Total Cleanup" is not.

Follow the project's CLI conventions: no colour-only distinction between the levels, the destructive
options confirmed by typing a word rather than pressing a key near the safe one, and each level's
consequences printed as prose before the prompt rather than as a table.

### Two risks stated up front, then built anyway

1. **Google publishes no Chrome for linux/arm64**, and only Chrome carries the Widevine CDM. On ARM
   hosts (Raspberry Pi, Graviton) Netflix, Disney+, Apple Music and Amazon Music will be cleanly
   disabled with a spoken reason. YouTube, Spotify and direct URLs work everywhere.
2. **You chose credential entry** for the four no-API services. Those logins routinely throw 2FA codes
   and CAPTCHAs. Passwords are encrypted at rest with a per-bot Fernet key, never logged (a root-logger
   redaction filter enforces this), the portal has an interactive OTP step so 2FA does not dead-end,
   and a session-import page is the escape hatch when a CAPTCHA blocks automation. Rebroadcasting these
   services into a TeamTalk channel is very likely a ToS violation; the README says so in paragraph one.

---

## Architecture

### The core problem: engines that are not mpv

mpv plays a URL. **librespot and Chrome are already producing audio into the sink** — there is no URL to
hand mpv. So `Player` stops being a thin mpv wrapper and becomes a transport façade over engines,
keeping all its existing state (`track_list`, `track_index`, `state`, `mode`, `volume`, queue, prefetch,
recents). Exactly one engine is active at a time, which is also what keeps the audio topology sane.

**New `bot/player/engines/__init__.py`** — `PlaybackEngine` ABC modelled on the existing `Service` ABC:
`initialize/can_play/play/pause/resume/stop/set_volume/seek/get_position/get_duration/get_metadata/close`,
plus an `on_end` callback the engine fires. Unsupported ops raise a new `errors.UnsupportedOperationError`,
caught by `SeekBackCommand`/`SeekForwardCommand`/`SpeedCommand` in
[user_commands.py](../../Documents/teamtalk%20tv%20streamer%20and%20music%20bot/TTMediaBot/bot/commands/user_commands.py).

Three engines:

| Engine | File | Plays |
|---|---|---|
| `mpv` | `bot/player/engines/mpv_engine.py` | YouTube, YouTube Music, direct URLs — pure extraction from today's `Player`, zero behaviour change |
| `librespot` | `bot/player/engines/librespot_engine.py` | Spotify |
| `browser` | `bot/player/engines/browser_engine.py` | Apple Music, Amazon Music, Netflix, Disney+ |

**`Player` changes** in [bot/player/__init__.py](../../Documents/teamtalk%20tv%20streamer%20and%20music%20bot/TTMediaBot/bot/player/__init__.py):
`_play(arg)` becomes `_play(track)`; on an engine switch it calls `old.stop()` then
`self.audio.focus(new)`. Extract the queue-priority / SingleTrack / RepeatTrack block at `:698-717`
into `_advance_after_end()`, and add `on_engine_end(engine, reason)` which ignores callbacks from a
swapped-out engine. Engines never touch `player.state` — only `Player` mutates it, which is what keeps
`TTPlayerConnector` (voice transmission + status text) working with **no changes at all**.

**`Track` changes** — add `TrackType.External = 5` and a `Track.engine: str = "mpv"` attribute. External
tracks carry a stable identifier URI (`spotify:track:…`, `netflix://watch/80100172`) rather than a stream
URL, are constructed `_is_fetched=True`, and so never enter `_fetch_stream_data`. `Streamer` only accepts
`http/https/rtmp/rtsp` so these URIs cannot leak into direct-URL playback. Replace the hardcoded
`if self.service not in ("yt","ytm")` guard at
[track.py:70](../../Documents/teamtalk%20tv%20streamer%20and%20music%20bot/TTMediaBot/bot/player/track.py) with a
`getattr(service, "engine", "mpv") != "mpv"` check — chosen deliberately so `test_track_refresh.py`'s
`SimpleNamespace` mock keeps passing untouched.

**`Service` ABC** gains class-level defaults (`engine = "mpv"`, `requires_auth = False`,
`supports_audio_description = False`, `auth_status()`), so `YtService`/`YtmService` need no edits.
`ServiceManager.__init__` stops hardcoding two entries and builds the dict from config. The existing
`initialize()` loop that catches `ServiceError` → `is_enabled = False` + `error_message` is exactly the
mechanism that makes "Chrome is unavailable on arm64" and "you are not signed in to Netflix" show up in
`sv` output with a readable reason — reuse it, do not replace it.

External services implement `search()` returning `TrackType.External` tracks, and `get(process=True)` as
the identity function (nothing to resolve). `download()` raises `UnsupportedOperationError` — no DRM
stream is ever written to disk.

### Audio topology

**One null sink, one active producer.** `entrypoint.sh` keeps its current shape but renames the sink to
`StreamerBotSink` and adds `Xvfb :99 -screen 0 1280x720x24` + `export DISPLAY=:99` before `exec "$@"`.

- mpv → `ao=pulse`, default sink (unchanged)
- Chrome → `PULSE_SINK=StreamerBotSink` in the Playwright launch env
- go-librespot → ALSA backend; install `libasound2-plugins` and ship `/etc/asound.conf` mapping
  `pcm.!default` to pulse

Multiple sinks were rejected because
[bot/sound_devices.py](../../Documents/teamtalk%20tv%20streamer%20and%20music%20bot/TTMediaBot/bot/sound_devices.py)
picks the TeamTalk input device **by list index** — adding sinks would shuffle indices and silently break
every existing bot's `config.json`. As de-risking, add `SoundDevicesModel.input_device_name` and prefer
name matching, falling back to the index.

New `bot/audio/pulse.py` — `PulseMixer` wrapping `pactl -f json` (Debian 13 ships PulseAudio 17, which
supports it; no new dependency). `mute_all_except(pids)` on every engine switch is the safety net for
"Chrome kept a paused-but-unmuted ad playing".

### YouTube: cookies → OAuth device code

In [youtube_bridge/server.mjs](../../Documents/teamtalk%20tv%20streamer%20and%20music%20bot/TTMediaBot/youtube_bridge/server.mjs)
delete `getBotCookieFile`, `cookieFileKey`, `netscapeCookiesToHeader`, `getWebSessionData`. `getSession`
becomes keyed on `bot_id` + credentials-file mtime and uses youtubei.js's own auth:

```js
const yt = await Innertube.create({ cache: new UniversalCache(true, authDir), ... });
yt.session.on('update-credentials', ({credentials}) => writeCreds(authFile, credentials));
if (creds) await yt.session.signIn(creds);   // silent, self-refreshing
```

New endpoints `/auth/start` (returns `{verification_url, user_code, expires_in}` from the `auth-pending`
event), `/auth/status`, `/auth/signout`. Anonymous fallback is preserved — with no credentials the
session is created without `signIn()` and search still works, exactly like today's cookie-less path.

**Container change:** the shared infra container mounts `bots/` `:ro` today; token persistence needs
`:rw`. `shared_youtube_mount_is_current()` in both `streamerbot.sh` and `update.sh` must be updated to
match, or it will recreate the container on every launch forever. The strict `bot_id` regex stays as the
containment boundary.

### Spotify

**go-librespot** (devgianlu), not Rust librespot — it exposes a local HTTP + WebSocket control API
(`/player/play|pause|resume|seek|volume`, `/status`, and a WS event stream). Rust librespot has no
control API. It has arm64 releases, so **Spotify works on ARM even though the browser services do not.**

**Pinned to 0.9.1, and the version matters.** Sign-in uses `credentials: type: device_auth`, a
code-and-URL flow with no browser redirect and no registered developer application — the same shape as
the YouTube device code, which is what a blind user on a headless server needs. That credential type
**does not exist in 0.9.0**, which accepts only `zeroconf`, `interactive` and `spotify_token`; it landed
in 0.9.1 together with "make device auth code available on API", which is what exposes the code at
`GET /auth/code` for the bot to read out. Building against the upstream README without checking gets
you a daemon that exits immediately with `unknown credentials: device_auth`, because that README
documents `master` rather than the latest release.

Track ends are detected by polling `/status`, not by holding the WebSocket. A WebSocket client is a new
dependency for one event, and the sub-second polling delay is the same order as the gap mpv already
leaves between tracks. Only a genuine end-of-track advances the queue: a pause from the Spotify app, a
handover to another engine, or the daemon restarting must not, or the bot would skip a track every time
any of those happened.

`LibrespotEngine` supervises the daemon (exponential backoff restart, output never inherited so the
credentials blob cannot reach a log) and drives playback. `SpotifyService` uses the Spotify Web API for
search and album/playlist expansion. Requires **Premium** for playback.

**The two halves authenticate separately, and this was not obvious.** Playback pairs the user's account
with a device code and needs no registered application at all. Search does need one: go-librespot could
mint a Web API token from its own session in 0.9.0 via `POST /token`, which would have removed the
requirement entirely, but **0.9.1 deleted that endpoint in the same release that added the device auth
flow** the engine depends on. There is no version that has both. So search falls back to the client
credentials flow, which needs a `client_id` and secret the operator registers once at
developer.spotify.com — app-level credentials, no user login. The engine path is tried first, so this
becomes free again if the endpoint ever returns.

The `client_id` sits in config; **the secret goes in the encrypted `SecretStore`, never in
`config.json`.** Without both, search is unavailable and says so in a message that distinguishes the two
halves, because a user told only "Spotify needs setup" will go looking at the wrong one. A pasted
Spotify link still plays with no application configured.

### Netflix / Disney+ / Apple Music / Amazon Music

Playwright driving **real Google Chrome** (`channel="chrome"`, so no `playwright install` and ~400 MB
saved), **headful under Xvfb** — headless Chrome produces no audio and is detected by Netflix. One
long-lived Playwright instance owned by a dedicated worker thread with a `queue.Queue` command channel,
because the sync API is not thread-safe and the bot calls in from the mpv event thread, command threads
and `TaskProcessor`. Persistent `BrowserContext` per service at `data/browser/<service>/`.

Per-site DOM knowledge is isolated in `bot/services/web/{netflix,disney,apple_music,amazon_music}.py`
behind a `WebServiceAdapter` ABC (`is_logged_in / login / list_profiles / select_profile / search /
watchlist / play / list_audio_tracks / set_audio_track`), so a site redesign is a one-file fix. Apple
Music uses the `MusicKit` JS object on `music.apple.com` rather than DOM scraping — far more stable.
Amazon Music has no such handle and is the most fragile of the four.

Audio description is a track in the site's own audio menu; the adapter matches on a per-locale substring
list it owns (never on the bot's translated strings), applied **after** the player loads, because the
menu is not populated before then.

### Auth portal and secret storage

`bot/modules/auth_portal.py` — stdlib `ThreadingHTTPServer` + hand-rolled router, matching the repo's
existing framework-free style. Every page requires `?t=<token>`; tokens are `secrets.token_urlsafe(32)`,
TTL'd, constant-time compared, and minted **only** by a TeamTalk command from a user who already passed
`CommandProcessor.check_access`. No token → 404, not 403.

`bot/auth/store.py` — `SecretStore` over **Fernet** (new `cryptography` dependency), per-field encryption
so a partial read leaks nothing:

```
data/secrets/portal.key            32 bytes, 0600
data/secrets/credentials.enc       {"entries": {"netflix": {"username": "gAAAA…", "password": "gAAAA…"}}}
data/browser/<service>/            Chrome profile (cookies live here)
data/librespot/credentials.json
data/youtube_auth/credentials.json
```

**Credentials are per bot, and that is a requirement, not an accident.** Two bots on the same host
must never share a YouTube, Spotify, Apple Music, Amazon Music, Netflix or Disney+ login. The
architecture already gives this for free — each container is created with
`-v "${BOTS_ROOT}/${bot_name}:/home/streamer/StreamerBot/data"`, so every path in the table above
resolves inside that one bot's directory — but it is written down here so no later phase quietly
introduces a shared location. Concretely:

- Nothing to do with credentials may live outside `data/`. No `/opt`, no image layer, no
  `/tmp/streamerbot-*` shared by pid.
- Each bot gets its own Chrome profile directory and its own Fernet key, so deleting one bot cannot
  sign another out and copying one bot's directory does not carry another's accounts.
- The **one** component that sees every bot is the shared YouTube bridge container, which mounts
  `${BOTS_ROOT}:/bots` because it serves all of them from a single Node process. It never holds
  ambient credentials: every request names a `bot_id`, and the strict
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` regex plus `path.join` under `BOTS_ROOT` is the containment
  boundary that stops one bot reaching another's tokens. That regex is load-bearing and must not be
  relaxed.
- `streamerbot.sh`'s "duplicate bot" action copies configuration but **not** `secrets/`, `browser/`,
  `librespot/` or `youtube_auth/`; the new bot starts signed out and says so.

Documented honestly: the key sits next to the ciphertext, so this defends against backups, `bots/`
tarballs and casual disclosure — **not** against an attacker with host root. An opt-in
`STREAMERBOT_SECRET_KEY` env mode (passphrase prompted at container start, never persisted) is the real
hardening path and gets its own menu item.

`bot/logger.py` gains a `SecretRedactingFilter` on the root logger scrubbing registered secrets plus
`password=` / `Bearer ` / `refresh_token` patterns. Playwright tracing, video and HAR are off by default.

`bot/auth/session.py` — `AuthJob` state machine
`queued → launching → filling → awaiting_otp | awaiting_captcha → success | failed(reason)`. The worker
calls `prompt.request_otp()`, which blocks on a queue; the portal's OTP POST unblocks it.
`awaiting_captcha` renders "we cannot solve this — use Import Session instead" with a link.

---

## Accessibility requirements (binding, not aspirational)

### Web portal — WCAG 2.2 AA, plus SC 2.4.13 adopted deliberately

Five architectural decisions that delete whole categories of bugs:

1. **Every step is a real URL with a real page load** — the 2FA step, profile picker, progress and
   success screens. A full navigation makes the screen reader announce the new `<title>`, the single
   most reliable "something changed" signal that exists.
2. **No modals, ever.** Disconnect confirmation is `/disconnect/netflix/confirm`. This removes focus
   trapping, focus return, Escape handling and inert backgrounds from scope entirely.
3. **Server-rendered errors are the source of truth.** `novalidate` on every form.
4. **JS is enhancement only** — the device-code screen gets a manual "Check status" button, the progress
   screen a manual refresh link.
5. **No `<meta http-equiv="refresh">` anywhere** — it violates SC 2.2.1/2.2.4 and makes the screen
   reader re-read the whole page every tick.

**Page titles** are the primary state signal, front-loaded, app name last:
`Enter code BCDF-GHJK at google.com/device - StreamerBot`. **Hard rule:** any page rendering validation
errors is prefixed `Error: `.

**The OTP screen and SC 3.3.8 Accessible Authentication.** Both the password and OTP steps *are*
cognitive function tests; we pass via the *Mechanism* exception, and only if:
`autocomplete="one-time-code"` / `"username"` / `"current-password"` are present, **paste works**
(no `onpaste` blocking, no `user-select: none`), there is a single input for the whole code, and an
explicit submit button. **Forbidden:** splitting the code into six single-character boxes (breaks paste,
breaks autofill, announces "edit blank" six times, and the auto-advance JS fights the screen reader
cursor); auto-submitting at N characters (fires mid-review when a blind user arrows back to verify);
any CAPTCHA; `maxlength` on the password field ever.

**The device code** gets two parallel representations — `<p class="device-code" aria-hidden="true">` for
sighted users, and a visually-hidden sibling reading `Your code is: B, C, D, F, dash, G, H, J, K.`
Comma-space is the reliable cross-AT technique to force per-character reading; JAWS re-joins
space-separated capitals, and `aria-label` on a roleless element is not consistently exposed. Symbols are
spelled as words. Any code we generate ourselves uses Crockford Base32 (no I, L, O, U).

**Focus management:** nothing on normal page load (autofocus skips the h1 and instructions — exactly the
content a blind user needs); `#error-summary` on a page with errors; nowhere on async success, then
navigate. Never move focus except from an explicit user action or a page load. No positive `tabindex`,
nothing sticky.

**Status list:** the failure mode is six buttons all named "Connect". Each row is an `<h3>` (so H-key
navigation works) plus visually-hidden text inside the button — `Connect<span class="visually-hidden">
Netflix</span>` — not `aria-label`, because the hidden-span form is safe by construction for SC 2.5.3
Label in Name and survives translation workflows. Status is text first (`Not connected`), icon
`aria-hidden`, colour decorative — SC 1.4.1 satisfied by construction.

Portal HTML is **built from translated fragments in Python**, not shipped as static template files, so
Babel's `translate` keyword extractor sees the strings.

### Corrections from the Phase 3 accessibility review

The portal spec above was reviewed before any code was written. Five of its "settled" decisions were
wrong and are overturned here. The originals are left in place above so the reasoning is traceable.

1. **The device code must not be `aria-hidden`.** `<p aria-hidden="true">BCDF-GHJK</p>` is absent from
   the accessibility tree, so the NVDA and JAWS virtual cursor cannot reach it: a blind user cannot
   select or copy the code, only memorise nine characters from one spoken pass. That is exactly the
   transcription burden SC 3.3.8 exists to remove, and copy-paste is the Mechanism we rely on to pass
   it. Replaced with a **`readonly` (never `disabled`) text input** carrying the code as its value,
   `aria-describedby` pointing at the visually-hidden comma-spelled sibling. This keeps both
   representations, restores copy-paste, and adds native left/right character review at the user's own
   verbosity — better than a fixed pre-spelled string they cannot replay.
2. **The device code must not be in the `<title>`.** eSpeak, Vocalizer and Eloquence each mangle
   `BCDF-GHJK` differently as an attempted word, across seven shipped languages, and a title is
   announced once and is awkward to replay. The one string that must be transcribed correctly was in
   the one place hardest to re-read. Title carries **state**, body carries **data**:
   `Connect YouTube: enter your code - StreamerBot`. Front-loaded titles stay everywhere else.
3. **`Connect<span class="visually-hidden">Netflix</span>` is replaced by a visible service name.**
   Three failures: the accessible name can concatenate to "ConnectNetflix" without a literal space
   inside the span; building it from fragments hands translators a bare "Connect" with no object,
   which is unbuildable in Turkish ("Netflix'e bağlan" — verb last, case suffix on the name) and has
   agreement problems in Arabic and Russian; and a visibly named control is simply better for everyone.
   One translatable string, `_("Connect %(service)s")`, and SC 2.5.3 then passes trivially because the
   visible and accessible names are identical.
4. **Service rows are `<h2>`, not `<h3>`.** `<h1>` → `<h3>` skips a level (SC 1.3.1) and breaks the
   outline NVDA's elements list builds. The H key walks every level, so `<h3>` bought nothing.
5. **Connect and Disconnect are `<a href>`, not `<button>`.** Both navigate to a URL and render a page.
   Screen reader users navigate by control type and build a model from the role; a "button" that turns
   out to be a page load is a small betrayal repeated six times. The only genuine `<button>` is the
   POST submit on the disconnect confirm page, which must stay POST so link prefetchers and antivirus
   proxies cannot fire it.

Also adopted from the review:

- **Token lifetime becomes 20 hours or more.** A `?t=` token is a time limit on user activity, so
  SC 2.2.1 Timing Adjustable applies at AA. With no JS we cannot warn before expiry, so the clean fit
  is WCAG's own **20 Hour Exception** — no UI needed at all. `token_ttl` defaults to 72000, not 3600.
- **`<meta name="referrer" content="no-referrer">`** on every page. The token is a bearer credential
  sitting in the URL, and the YouTube page links out to google.com, which would otherwise receive it
  in the `Referer` header. A security fix, not an accessibility one.
- **RTL is real here**: `ar` already ships. `lang` and `dir` come from the negotiated locale, CSS uses
  logical properties throughout (`margin-inline-start`, never `left`), `dir="ltr"` goes on the device
  code and OTP inputs, and inline `<span lang="en">` wraps hostnames and codes so the synth does not
  transliterate them.
- **No `role="alert"` on a server-rendered error summary.** On a full page load it collides with the
  focus move and the `Error: ` title prefix, giving a double announcement in JAWS and inconsistent
  behaviour in NVDA. Focus moves to `#error-summary` and that is enough. Summary link `href` targets
  the **input's** id, and the summary text and the field error text must be byte-identical.
- **OTP input is `type="text"` with `inputmode="numeric"`, never `type="number"`** — a spinbutton
  announces as one, arrow keys change the value instead of moving the caret, and leading zeros are
  dropped. **No `maxlength`** either: a pasted code with a trailing space is silently truncated.
- **No skip link and no `tabindex="-1"` on `<main>`** while the header is a single line of text; both
  would add a tab stop that lands nowhere.
- **SC 3.2.4 Consistent Identification**: one verb across all six services in all seven languages —
  not "Connect Netflix" beside "Link Spotify" and "Authorise YouTube".

Nine pages were missing and are added: the expired device code, `resend` OTP, expired OTP, sign-in
timed out, cancel a sign-in, a rate-limit page, **410 Gone for an expired token versus 404 for one
that never existed** (with different recovery copy), a 500 page, and a `/failure/<service>` sibling to
`/success`. Every one of these is currently an invisible dead end. A bare 404 is a genuine dead end for
a blind user who cannot inspect the URL, so every error body carries an explicit recovery path.

The review also re-raised credential entry for the four no-API services as a credential-harvesting
pattern, with the added point that **browser and OS password managers — which blind users lean on
heavily — will offer to save Netflix credentials against the portal's own domain and later autofill
them into the wrong form.** This is a known, accepted decision (see "Two risks stated up front"), not a
new finding; noted here because the password-manager consequence was not previously written down.

### TeamTalk chat — one-shot audio announcements

Plain text only, no emoji, no arrows, no markdown (`*` is spoken "asterisk"). Front-load the type:
`Error. Netflix sign-in failed. Wrong password.` One message per event — three in a row means the user
hears the tail of the first and the head of the third. Yes/no prompts state the accepted replies and
accept `yes/y/no/n` case-insensitively; an unrecognised reply **re-asks the whole question** rather than
saying "invalid". Numbered choices are number-first, nine or fewer, `0. Cancel` always last. Write
"Disney Plus", not "Disney+". Echo the selection back. Say what a link is for *before* the URL. State
timeouts up front. A `menu` command re-sends the last prompt verbatim.

### streamerbot.sh — screen-reader-first CLI

Keeps the existing plain-`echo`/`read` paradigm (no dialog/whiptail/ncurses) and improves on it:

- **Stop calling `clear`.** Scrollback is what a screen reader user relies on to review what just
  happened. Print a blank line and a title line instead. *(This is a deliberate change from
  `ttbotdocker.sh`, which clears on every screen.)*
- No `====` separators or ASCII banners — `====` is read as "equals equals equals equals".
- Nine or fewer items per menu; `b) Back` and `q) Quit` last, in the same position, on every menu.
- Echo the choice back: `You selected 3, Restart bot.` On invalid input restate the valid range.
- **No spinners or `\r` progress bars** — a self-rewriting line is re-read continuously and is
  effectively a denial of service. Print discrete `Step 2 of 4. Updating configuration.` lines.
- Destructive actions require a **typed word**, not `y/N` (capitalisation conveys nothing to TTS):
  `Type delete to confirm, or press Enter to cancel:`
- Prefix status lines with the word: `OK.`, `Error.`, `Warning.` Never colour alone.
- **Non-interactive flags for every menu item** (`streamerbot --status`, `--restart-all`,
  `--services`) — experienced screen reader users skip menus, and flags are faster and more reliable.

---

## Configuration and repo constants

**New `project.env`** (repo root, shell-sourceable) is the single source of truth:

```
STREAMERBOT_REPO_OWNER=          # <-- your GitHub username
STREAMERBOT_REPO_NAME=StreamerBot
STREAMERBOT_BRANCH=main
STREAMERBOT_IMAGE=streamerbot
TTSDK_VERSION=5.22
TTSDK_URL_X86_64=
TTSDK_URL_ARM64=
GO_LIBRESPOT_VERSION=
```

Every shell script sources it after computing `SCRIPT_DIR`; `bot/app_vars.py` gains a 12-line stdlib
parser and interpolates `repo_url` into `about_text`. `streamerbot.sh` prompts and `sed -i`s the owner on
first run if empty; `update.sh` and `auto_updater.sh` exit with a clear message instead (they run
non-interactively).

This replaces the hardcoded `REPO_OWNER`/`REPO_NAME`/`BRANCH` at `update.sh:304-306`, the systemd unit
names in `masc.sh`, the clone URLs in `install*.sh`, and the three literal TeamTalk DLL release URLs at
`update.sh:552/556/559` — those last ones disappear entirely, because the SDK now enters at image build
time.

**Config schema** (`bot/config/models.py`, pydantic): `ServicesModel` gains `spotify`, `netflix`,
`disney`, `apple_music`, `amazon_music`, `audio_description`; `ConfigModel` gains `auth_portal`.
`YtModel.cookiefile_path` is kept but unread, for migration. `ConfigManager.version = 2` with a `to_v2`
migrator. `bot/cache.py` version 2 adds `ad_preferences` (per-TeamTalk-username AD choice) and
`web_profiles`.

---

## Docker image

`FROM debian:trixie-slim`, arch-aware via `ARG TARGETARCH`.

Two things will break the build if missed, so handle them explicitly in Phase 0:

1. **PEP 668** — Debian 13 marks the system interpreter externally-managed, so `pip install` fails.
   Create `/opt/venv` and `ENV PATH=/opt/venv/bin:$PATH`.
2. **pydantic v1 has no wheels for Python 3.13** (trixie's Python). The code uses `.dict()` and bare
   `BaseModel` — that is v1 API. Pin `pydantic>=2` and change the two `.dict()` call sites in
   `bot/config/__init__.py` to `.model_dump()`.

Other changes: `libmpv2`/`libmpv-dev` (trixie names) satisfy the vendored `mpv.py`; TeamTalk SDK 5.22
fetched from bearware.dk at build time by `$TARGETARCH` into `/opt/teamtalk` with `LD_LIBRARY_PATH` set;
`google-chrome-stable` from `dl.google.com` **on amd64 only**, writing `/etc/streamerbot-browser-available`
so `BrowserEngine.initialize()` can raise a clean translated `ServiceError` on ARM; `xvfb`,
`fonts-liberation`, `libnss3`, `libgbm1`, `libasound2-plugins` + `/etc/asound.conf`; pinned
`go-librespot` binary for both arches; `pip install playwright` with **no** `playwright install`.
User `streamer` (uid 1000) at `/home/streamer/StreamerBot`. Keep the existing two-stage layer discipline
(stable deps above `ARG CACHEBUST`, `COPY . .` below) — the `youtube_bridge/package.json` npm layer
especially, since `youtubei.js` tracks `#main`.

**Verify `TeamTalkPy/TeamTalk5.py` against the 5.22 SDK.** It is a hand-maintained ctypes binding; if a
struct the bot touches changed, the symptom is silent memory corruption, not a clean error. Prefer the
`TeamTalk5.py` shipped in the SDK archive if there is one, and diff carefully.

---

## streamerbot.sh menu tree

```
StreamerBot manager

1) Create bot
2) Manage bots
3) Service logins and auth portal
4) Rebuild image
5) Check for updates
6) Auto-updates on or off
7) Clean Docker cache
8) Shared YouTube server
9) Uninstall everything
q) Quit
```

`2) Manage bots` keeps every existing item except "Update Cookies (All Bots)" (deleted along with
`get_cookies()` and the `cookies.txt` bind mount), and gains **Clear browser profiles**, **Show
per-service status** (reads a small `bots/<name>/service_status.json` the bot writes on state change —
no `docker exec` round-trips) and **Rotate auth portal secrets**. Paginate to stay at nine or fewer.

`3) Service logins and auth portal` is new: show portal URLs for all bots or one, rotate a token, sign
out a service, import session cookies (reusing the old paste-with-CTRL+D UX verbatim — users already
know it), YouTube sign-in status.

**Startup behaviour change you asked for:** `ttbotdocker.sh:1862-1865` runs the whole of `update.sh` on
every launch. Replace with `check_for_updates_passive`, which calls a new `update.sh --check-only`
handled *before* `acquire_update_lock` and before any fetch — a 5-second `git ls-remote` compared against
`git rev-parse HEAD`, printing exactly:

```
An update is available. Use Check for updates to install it.
```

or nothing. No lock, no fetch, no backup, no rebuild. Menu item 5 runs the full update as today.

---

## Update system

Keep the entire proven shape: flock at `/tmp/streamerbot_update.lock`, back up `bots/` to a tmpdir,
`git reset --hard`, restore, single re-exec guard, `perform_image_rebuild()` writing `update_in_progress`
/ `update_success` marker files that the Python bot reads and broadcasts as TeamTalk channel messages
([bot/__init__.py:116-128,152-164](../../Documents/teamtalk%20tv%20streamer%20and%20music%20bot/TTMediaBot/bot/__init__.py)),
stop-all → rebuild → recreate → restart → five-minute health check. `auto_updater.sh` keeps its 20-second
`git ls-remote` poll, `.no_update` pin and self-re-exec; the unit becomes `streamerbot-updater.service`.

Changes: repo constants from `project.env`; `--check-only`; delete the TeamTalk_DLL download block
(`:544-575`) since the SDK now enters at build time; pass `--build-arg` for the SDK and librespot URLs;
and **exclude `browser/` from the update backup** — `bots/` is kilobytes today but becomes gigabytes with
Chrome profiles, which would balloon every update. `chown -R 1000:1000` after any restore, or the Chrome
profiles break.

---

## Delivery order

Each phase has an exit criterion you can actually check.

| Phase | Work | Exit criterion |
|---|---|---|
| **0** ✅ | Global rename, `project.env`, pydantic v2 migration, Debian 13 + venv Dockerfile, TT SDK 5.22, `streamerbot.sh` with passive update check | **DONE** — image builds; in-container Python 3.13.5 / pydantic 2.13.5, TeamTalkPy + libmpv import, Chrome 152, go-librespot, PulseAudio null sink with monitor as default source, Xvfb 1280x720, Chrome launches under it, 13 tests pass (2 host-only skips) |
| **1** ✅ | Engine abstraction, mpv only. `TrackType.External`, `Track.engine`, `_advance_after_end`, name-based sound device | **DONE** — 36 tests pass; the original 13 untouched, incl. `test_player_stream_retry.py` and `test_track_refresh.py`. Note: those two tests pin `on_end_file`, `self._player` and `_play(url_string)` onto `Player`, so mpv's event handling stayed there and only transport moved behind the engines — a deliberate departure from the plan's "move `on_end_file` into `MpvEngine`" |
| **2** ✅ | YouTube OAuth — bridge `/auth/*`, `yl` command, portal `/youtube` page, `:rw` mount fix | **DONE** (portal page deferred to Phase 3, which builds the portal) — verified against live Google: real device code returned, status tracked pending, `bot_id` traversal refused, anonymous search still resolved, signout cleared tokens. 46 tests pass. Two live-only bugs found: the bridge died with exit 255 because Node makes the device flow's background rejection fatal, taking YouTube down for **all** bots (fixed with process-level handlers); and sign-in hit EACCES because `update.sh` ran `chown -R` to the host user and `chmod -R 777` over the whole repo, both locking uid 1000 out of `bots/` and leaving credentials world-writable (fixed by pruning `bots/` and adding `ensure_bot_data_ownership`) |
| **3** ✅ | Auth portal + `SecretStore` + `AuthJob`/OTP + log redaction + `li`/`ap`/`lo` commands | **DONE** — 111 tests pass, CI green. Portal reviewed for accessibility *before* coding, which overturned five decisions the plan had marked settled (see "Corrections from the Phase 3 accessibility review"); the `aria-hidden` device code was the serious one — it would have been uncopyable by exactly the users who most need to copy it. **Deferred to Phase 5**, which builds the browser engine: the sign-in worker itself (so `/connect` for the four credential services reaches a job reporting the feature is not ready) and OTP resend |
| **4** ✅ | Spotify — go-librespot, `LibrespotEngine`, `SpotifyService`, PKCE through the portal | `sv sp` then `p <song>` plays into TeamTalk; `n`/`b`/`qa`/`m rnd` all behave; switching to `sv yt` mid-queue swaps engines cleanly |
| **5** ✅ | Browser engine + Netflix only — Xvfb, Chrome, Playwright, login with OTP, profiles, AD prompt | Sign in, pick a profile, `p <title>`, get asked about audio description, hear the AD track |
| **6** ✅ | Disney+, Apple Music, Amazon Music adapters against a proven engine, plus the **gamdl download wrapper** | Each plays with AD where the service offers it; `dl` on an Apple Music album uploads one zip to the channel |
| **7** ✅ | `streamerbot.sh` polish, backup exclusions, README/CHANGELOG rewrite, publish to your fork | `git clone` + `./streamerbot.sh` works from a clean host |
| **8** ✅ | Collapse the inherited history to a single commit, rewrite `README.md` as a fork with its own feature list | `git log` shows only your commits; README describes StreamerBot, not TTMediaBot; `LICENSE` still carries the upstream copyright |

Phase 0 is the riskiest to skip and the cheapest to verify. Phase 4 gives the engine abstraction its
first real workout on the *easier* of the two external engines, before Chrome.

---

## New chat commands

Checked against the existing 34 user + 17 admin codes — no collisions.

**User** (`commands_dict`): `li` login status / portal link · `w` watchlist · `ep` episodes ·
`pf` profile picker · `da` audio-description preference.

`li` takes an optional service, and that is the recovery path for anything skipped during setup.
Nobody has to get every account connected while creating the bot, and nobody has to delete and
recreate a bot to add one later: with the bot sitting in a channel, a message starts the flow for
just that service.

- `li` alone — status for all six: connected, not connected, or expired, one line each, plus the
  portal link.
- `li nf` / `li dp` / `li sp` / `li am` / `li az` / `li yt` — begin sign-in for that service only.
  YouTube answers in the channel with the device code and URL (the same flow as the admin `yl`);
  the other five mint a single-use portal token and reply with the link to that service's page.
- `li` on an already-connected service reports it and asks whether to replace, so a stray message
  cannot silently sign someone out.

Access is `check_access`-gated like any other command, and the reply goes to the requesting user
rather than the channel, so a device code or portal link is never broadcast to everyone present.
**Admin** (`admin_commands_dict`): `yl` YouTube device-code sign-in · `lo` sign out a service (wipes
credentials + browser profile) · `ap` portal status / rotate / on / off · `es` engine health
(mpv, librespot pid + API, Chrome contexts, Xvfb, portal, active engine).

`w`/`ep` load results into the existing `pending_search_results` so `sl N` selects them — reusing
`SearchResultsCommand` plumbing rather than inventing a second selection mechanism.

**Audio description prompt** reuses the repo's own interactive pattern (`pending_ads_option` at
[commands/__init__.py:38,118-121](../../Documents/teamtalk%20tv%20streamer%20and%20music%20bot/TTMediaBot/bot/commands/__init__.py)):
a `pending_ad_prompt` dict checked in `_run` before `parse_command`. New
`bot/modules/audio_description.py` resolves preference in order user-pref → config default, prompts
`1. Yes / 2. No / 3. Yes, and remember / 4. No, and remember`, and runs a `threading.Timer` that falls
back to the default so **playback never hangs on an unanswered prompt**. A single
`Command._play_with_ad_gate()` hook keeps the logic in one place across
`PlayPause`/`SelectSearchResult`/`SelectTrack`.

Every new string wrapped in `translate(...)`; run `python tools/compile_locales.py` and commit the
updated `.pot` + `.po`. **Rename the gettext domain carefully** — `locale/TTMediaBot.pot` and the seven
`TTMediaBot.po` files must be renamed together or Babel creates empty catalogs and all existing
translations are lost.

---

## Verification

**Automated**, staying inside the repo's two existing conventions — stdlib `unittest` with
`object.__new__` + `Mock`, and `tests/deployment/bash_sandbox.py` for shell:

- `test_engine_dispatch.py` — engine switch calls `old.stop()` exactly once; a stale engine's
  `on_engine_end` is ignored; `_advance_after_end` honours queue priority for an external track
- `test_track_external.py` — External tracks never enter `_fetch_stream_data`; `refresh_stream` raises
  for non-mpv engines; the yt/ytm path still works
- `test_secret_store.py` — round-trip, 0600 perms, wrong-key failure
- `test_auth_session.py`, `test_auth_portal.py` — state transitions and OTP hand-off; server on port 0
  driven with `urllib.request`: missing/wrong/expired token → 404, valid → 200. No browser.
- `test_librespot_engine.py` — a `ThreadingHTTPServer` stub for the go-librespot API
- `test_pulse_mixer.py` — recorded `pactl -f json` fed through a patched `subprocess.run`
- `test_browser_adapters.py` — **pure parts only**: URI parsing, and AD track-name matching
  (`"English [Audio Description]"` yes, `"English [Original]"` no). Never launch Chrome in a unit test.
- `tests/deployment/test_streamerbot_runtime.py` — shim `git`, assert the "update is available" line
  appears exactly once and that **no** `docker build` or `git fetch` shim was invoked on launch. This is
  the regression test for the startup-behaviour change.
- `youtube_bridge/test/auth.test.mjs` (`node --test`) — `getBotAuthFile` rejects `../`, empty and
  overlong ids; credentials written 0600.

**Manual**, written into `tests/MANUAL.md` as a numbered accessible checklist:
Chrome + Widevine playback; a real 2FA flow end-to-end; Spotify Premium gating; TeamTalk audio audit
(does the channel actually hear it, at what latency); and a **screen-reader pass over the portal with
NVDA** — tab order, the device-code spelling, the OTP field accepting paste, error-summary focus, and
every page title.

---

## Phase 8 — own the history, own the README

Runs last, after every feature phase has landed and CI is green. It rewrites published history, so it
is deliberately the final step: doing it earlier would invalidate every commit hash the later phases
were built on.

### Collapse the inherited history

The repository currently carries **914 commits from about 20 upstream authors** (Gumerov Amir
Eduardovich, JoaoDEVWHADS, Beqa Gozalishvili, cyrmax, and others). Only the StreamerBot commits are
yours. The goal is a history that starts with your work.

Method — squash to a single root commit rather than trying to excise commits individually:

```
git checkout --orphan release
git add -A
git commit -m "StreamerBot 1.0"
git branch -M release main
git push --force origin main
```

An orphan branch has no parent, so the 914 ancestors simply stop being reachable and GitHub garbage
collects them. Filtering by author instead (`filter-repo --commit-callback`) would leave a broken tree,
because your commits are diffs against theirs and cannot apply without them.

Three things to be clear about before running it:

- **`LICENSE` must stay exactly as it is.** MIT requires the copyright notice be retained in the
  software. Deleting git *history* is fine; deleting the notice is not. The file keeps
  "Copyright (c) 2021 Gumerov Amir Eduardovich" and gains your own line beneath it.
- **It is a force-push.** Anyone who has cloned or forked the repo gets a history that no longer
  matches theirs. Right now that is nobody but you, which is exactly why this belongs at the end
  rather than after the repo has users.
- **The upstream remote should be dropped** at the same time (`git remote remove upstream`), since
  `git pull upstream` would drag all 914 commits straight back in.

Take an archive tag first (`git tag archive/pre-squash && git push origin archive/pre-squash`) so the
old history is recoverable for as long as you want it, and delete the tag once you are satisfied.

### Rewrite README.md

Replace it outright rather than editing around the existing text. The current README documents
TTMediaBot: its services, its config keys, its install steps, its contributors and its screenshots —
almost none of which survive this project.

The new one covers, in this order:

1. **What StreamerBot is** — an accessible TeamTalk streaming bot, in two sentences.
2. **Fork notice**, plainly worded and near the top: a fork of
   [TTMediaBot](https://github.com/gumerov-amir/TTMediaBot) by Gumerov Amir Eduardovich, further
   developed from [JoaoDEVWHADS/TTMediaBot](https://github.com/JoaoDEVWHADS/TTMediaBot), MIT licensed,
   substantially rewritten.
3. **What this fork adds** — the honest feature list, written from what actually shipped:
   Spotify, Netflix, Disney+, Apple Music and Amazon Music alongside YouTube; audio description
   support where the service offers it; YouTube OAuth device-code sign-in replacing `cookies.txt`;
   the web auth portal with encrypted secret storage; the playback engine abstraction; TeamTalk
   SDK 5.22a; a Debian 13 image; hourly update checks; and a screen-reader-first CLI.
4. **Requirements and install**, offering both routes and leading with the one that assumes least:

   **Route A, the one-shot installer** — for a fresh host where the user has no git, no Docker, and no
   clone. `install_git_clone.sh` is self-contained: it installs git if missing, clones
   `kcrpine/StreamerBot`, installs `jq`, `curl` and `tar` if missing, installs Docker from
   `get.docker.com` and starts the service, fixes ownership and permissions for the invoking user, and
   then `exec`s `streamerbot.sh`. Because the point is that the user does not yet have the repository,
   the README has to show fetching the script on its own rather than running it from a checkout:

   ```
   curl -fsSL https://raw.githubusercontent.com/kcrpine/StreamerBot/main/install_git_clone.sh -o install_git_clone.sh
   less install_git_clone.sh
   sudo bash install_git_clone.sh
   ```

   Show the `less` step and say plainly why it is there: this pipes a script from the internet into a
   root shell, and reading it first is the reasonable precaution. Do not offer a
   `curl … | sudo bash` one-liner, which removes the chance to look.

   State what it needs and what it does not: a Debian, Ubuntu, Fedora or Arch host with `sudo`, and
   nothing else — no TeamTalk SDK download, no `TeamTalk_DLL` directory, no `cookies.txt`. The SDK now
   arrives inside the Docker image. Note that it finishes by launching the manager, so the first run
   continues straight into creating a bot, and that re-running it on a host that already has the repo
   is safe.

   **Route B, manual** — for users who already have git and Docker and would rather clone themselves:

   ```
   git clone https://github.com/kcrpine/StreamerBot.git
   cd StreamerBot
   ./streamerbot.sh
   ```

   Mention that `streamerbot.sh` elevates itself with `sudo` when needed, and that on first run it asks
   once for the GitHub username to take updates from, writing the answer to `project.env`.
5. **Configuration** — `project.env` and the per-bot config.
6. **Accessibility** — that the portal targets WCAG 2.2 AA and the CLI is written for screen readers,
   since that is the point of the project rather than a footnote.
7. **Contributing**, and how to get write access. Two paths, stated plainly so nobody has to guess:

   - **Anyone can open an issue or a pull request.** No permission needed, and this is the normal
     route. Say that PRs run the full CI (tests, shell checks, image build) automatically and that a
     green run is what gets one merged.
   - **To become a collaborator with write access**, open an issue asking, or say so in a PR. Access
     is granted by invitation from the repository owner; it is not something a fork or a PR can
     confer on itself.

   Also point at the **Claude Code project shipped in `.claude/`** — the implementation plan, the
   accessibility hooks and the settings — for anyone intending to build on this or make changes. It
   is there so a contributor starts from the same plan and the same rules rather than reconstructing
   them, and `CLAUDE.md` at the root is the orientation for it. Note that the accessibility agents the
   hooks call are referenced rather than vendored and have to be installed separately, and that the
   hooks degrade to a printed reminder without them, so a fresh clone still works.

8. **License** — MIT, retaining the upstream copyright, plus the note that `mpv.py` is vendored
   AGPLv3 (a licensing inconsistency inherited from upstream, worth stating rather than hiding).

Everything else from the old README goes: the TTMediaBot name, the old install instructions, the old
service list, the contributor section and the old screenshots.

