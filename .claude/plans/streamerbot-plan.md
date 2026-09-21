# StreamerBot — multi-service accessible TeamTalk streaming bot

## Context

`C:\Users\kcrpi\Documents\teamtalk tv streamer and music bot\TTMediaBot` is a fork of TTMediaBot
(origin `JoaoDEVWHADS/TTMediaBot`). Today it streams **YouTube and YouTube Music only**, authenticated
by a Netscape `cookies.txt` file that the operator must manually re-export from a browser every few
weeks. It runs one Docker container per bot, managed by `ttbotdocker.sh`, on `python:3.10-slim-bullseye`
with TeamTalk SDK 5.8.1.

We are turning it into **StreamerBot**: same multi-bot Docker architecture, but

- **no more hand-exported cookie files** — YouTube signs in once through the portal and keeps itself
  signed in. (As written this said "no more cookie files" and "an OAuth device code that refreshes
  itself forever". Phase 9 retired the device code: YouTube answers 400 to OAuth-authenticated player
  requests. It plays as a real browser session, so cookies came back — but as a file the bot writes and
  rotates for itself, never one the operator re-exports every few weeks, which is what this bullet was
  really promising. See "Phase 9" and "YouTube: cookies → OAuth device code".);
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
as a prerelease tagged `build-<YYMMDD.HHMMSS>` in UTC — `build-260920.002545` is 20 September 2026 at
00:25:45 — and records in the release notes whether the checks passed. (As written this said
`build-<run number>-<short sha>`; the tag is now the time the run started. A hash identifies a commit,
which Git and GitHub already do better than a build tag can, but it says nothing about *when*, so
"which of these two builds is newer" needed a round trip through the repository to answer. The stamp is
computed once in a `stamp` job and reused by the test report titles, the CI image tag, the release and
the email, so everything in one run names the same instant; a same-second collision on two refs gets a
`.2` counter.) Two reasons this earns its place: every commit gets a downloadable artifact, and
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

**Every push adds a numbered entry to the CHANGELOG.** Not at release time, when nobody remembers
what changed or why: in the same commit as the work, while the reasoning is still in hand.

Entries live under an `## Unreleased` heading at the top of `CHANGELOG.md`, newest first, each one
opening with a number in square brackets:

```
## Unreleased

- **[003]** Connection and login failures now name the host, the port and the attempt number.
  They were logged already, but as "Connection failed" with no detail, which told a user nothing.
- **[002]** Unhandled exceptions in any of the bot's threads now reach the bot's log rather than
  stderr, where a container sends them nowhere. A playback thread dying used to be invisible.
- **[001]** Restoring an old backup removes its obsolete cookies.txt.
```

Rules that make the numbers worth having:

- **Numbers only ever go up, and are never reused**, even for a change that is later reverted. The
  revert gets its own number and says what it undid. A number is a permanent handle: "the [002]
  change" has to mean one thing forever, or referring to it in an issue is useless.
- **One entry per change, not per commit.** A fix split across three commits is one entry; three
  unrelated fixes in one commit are three.
- **Say what it does and why, not what was edited.** "Fixed thread.py" is worthless in six months.
  Anything surprising gets the reasoning, because that is the part not recoverable from the diff.
- **When a release is cut**, the `## Unreleased` block becomes that version's section and a fresh
  empty one is started. Numbering continues across releases rather than restarting, so a number
  identifies a change without needing a version alongside it.
- The numbers are for tracking, not for users. The prose in each entry is what a person reads; the
  number is what an issue or a commit message points at.

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

## Phase 9 — YouTube sign-in through a real browser session

Added 13 September 2026, after testing on a VPS. **This overturns a Phase 2 decision** — "no more cookie
files, YouTube signs in with an OAuth device code" — so the reasons it no longer holds are written down
first.

### Why the OAuth device code is being retired for playback

The test bot's log from 10 to 13 September records 3,577 failed stream resolution attempts, counting retries,
and not one successful stream fetch. After [012], [015] and [019]–[021], the pattern is stable and has two halves:

- **Signed in with the device code:** the player endpoint answers **400** to every OAuth-authenticated
  request. This is not a bug in how youtubei.js reads the sign-in. YouTube stopped serving playback to
  the TV-client OAuth grant that device-code sign-in produces, and yt-dlp removed its own OAuth login for
  the same reason. There is nothing to fix on our side.
- **Anonymous:** every client answers `LOGIN_REQUIRED: Sign in to confirm you're not a bot`. The host is
  on a datacenter address YouTube does not trust, and from such an address anonymous playback is
  refused.

So the device code signs the account in and then cannot be used to play anything, and anonymous use
cannot play anything from a VPS. That leaves the one credential YouTube still accepts for playback from
an untrusted address: **the cookies of a real, signed-in browser session, sent together with a
proof-of-origin token.** That is what this phase builds.

One caveat on the evidence, recorded so it is not over-read: the bot log cannot show which build the
*shared* YouTube container is running. Before starting, confirm with
`curl -s http://127.0.0.1:4417/health` that the bridge reports `pot_provider.reachable: true`. If it does
not, the container predates [015] and must be recreated first, since a missing token produces the same
`LOGIN_REQUIRED` and would make this phase look like it failed.

### What replaces what was asked for, and why

The request was: a text browser such as Lynx extracts the user's YouTube cookie, a scheduled task checks
whether it has expired and renews it, per bot; a user who plays a link while that is happening is told to
wait; and if a restart is needed, the bot restarts itself and asks them to resend the link. Most of that
stands. Four parts change, each for a reason that would otherwise surface as a failure in testing:

1. **Chrome, not Lynx.** Lynx runs no JavaScript, and Google's sign-in page does not work at all without
   it. Google also refuses sign-in from browsers it does not recognise. The image already ships real
   Google Chrome with a persistent profile per service per bot, driven by the browser engine from
   Phase 5, and signing in to Apple Music already works this way through the portal. YouTube becomes one
   more profile, at `data/browser/yt/`.
2. **The session is kept alive, not "renewed".** Nothing can renew a Google session on the user's
   behalf once Google has ended it; if that were possible it would be a security hole in Google. What
   *can* be done is to stop it ending unnecessarily. Google rotates part of the session
   (`__Secure-1PSIDTS` and `__Secure-3PSIDTS`) and treats a session that stops rotating as stale, and a
   live browser rotates them just by loading youtube.com. So the scheduled task loads youtube.com in the
   bot's own profile, lets the rotation happen, and exports the fresh cookies. A session Google really
   has ended — password changed, signed out elsewhere, a security check — can only be restored by the
   user signing in again, and the bot tells them so.
3. **The check is "does YouTube say we are signed in", not "has the cookie expired".** Google's cookie
   expiry dates are years away and mean nothing; sessions are ended server-side long before. A cookie
   that is unexpired on paper and dead in practice is exactly the failure a date check would miss.
4. **Cookies go in the bot's `data/`, not in `config.json`.** A cookie file is a full login to someone's
   Google account. The Phase 3 rule is that credentials live only under the bot's own `data/`, encrypted
   where possible, and never in `config.json`, which is copied by Duplicate Bot, read by the
   configuration check, and handled far more casually. `config.json` gets settings only — whether
   browser sign-in is enabled and how often to refresh — and never the cookies.

On restarts: the design should need none, and the restart path is kept as a last resort rather than the
normal route. That matters for a reason specific to this architecture: the YouTube bridge is **one
container shared by every bot on the host**, so restarting it to pick up one bot's new cookies would
interrupt playback for everyone. The bridge instead watches each bot's cookie file and rebuilds only that
bot's session when the file changes, the way it already watches `youtube_auth/credentials.json` today.

### The flow

**Signing in, once per bot.** `li yt` stops starting a device code and instead sends a portal link, the
same as `li am`. The portal page drives the bot's Chrome through Google's sign-in: email, password, then
whatever second step the account uses. Google's second step is usually a prompt on the user's phone
rather than a typed code, so the portal page has to say "approve the sign-in on your phone" and wait,
with a manual "I have approved it" button in keeping with the portal's no-auto-refresh rule, not only the
existing typed-code box. Once signed in, cookies are exported to `data/youtube_auth/cookies.txt` (mode
0600, directory 0700) and the bridge picks them up.

**The session-import page.** Phase 3's plan promised an "import session" escape hatch for when a CAPTCHA
blocks automated sign-in. **It was never built** — nothing in `bot/` implements it. It is now required,
because Google is more likely than Apple to refuse an automated Chrome outright ("This browser or app may
not be secure"). The page accepts a `cookies.txt` exported from the user's own browser, pasted into one
text area (the paste-with-Ctrl-D habit from the old TTMediaBot, adapted to a form), validates that it
contains a Google session, and stores it in the same place. It is not a failure mode that makes the
feature useless; it is the path that works when the automated one does not.

**Keeping it alive, per bot.** A thread in the bot process, alongside the existing periodic pre-warm, not
a cron job or a systemd timer. It runs inside the container where that bot's Chrome and profile already
are, so "per bot" comes free and nothing has to reach into another bot's directory. Every six hours
(configurable, `services.yt.session_refresh_hours`) it:

1. loads youtube.com in the bot's `yt` profile and waits for the page to settle;
2. asks the page whether it is signed in (`ytcfg` `LOGGED_IN`), which is the real check;
3. if signed in, exports the cookies, writes the file only if they changed, and logs one INFO line;
4. if not, marks the session as needing sign-in, logs a WARNING naming the bot, and stops trying until
   the user signs in again rather than retrying forever against a dead session.

It also runs **on demand** the first time a resolution fails with `LOGIN_REQUIRED` while a session
exists, rate-limited to once per ten minutes, because that failure is the most direct evidence the
session just went stale.

**The bridge.** Given a cookie file, `getSession` passes it as youtubei.js's `cookie` option. The
proof-of-origin binding changes with it: [015] binds the token to `visitorData`, which is correct for a
signed-out session, but a signed-in session is identified by its **DataSync ID**, and the token must be
bound to that instead. The signed-in-with-cookies attempts replace the OAuth attempts at the head of
`planPlaybackAttempts`, and the anonymous fallback stays behind them for hosts that do not need a
session at all.

**What the requester hears.** Per the TeamTalk chat rules: plain text, one message per event, to the
person who asked rather than the channel, because the channel does not need to know about someone's
account.

- A play or link request arrives while a refresh is running: *"YouTube sign-in is being renewed. Please
  wait, playback will start when it finishes."* The request is **held and played automatically** when the
  refresh completes, not dropped. Asking someone to resend a link they have already sent is avoidable
  work, and for a screen reader user retyping or re-pasting a URL is not trivial.
- The refresh finds the session dead: *"YouTube needs you to sign in again. Send li yt to get a link."*
  Not "service unavailable", which is what every one of these failures says today and which tells the
  user nothing they can act on.
- Only if a restart genuinely proves necessary, which the design above is meant to prevent: the bot
  restarts **itself**, not the shared bridge, and after reconnecting sends *"I restarted to finish
  renewing YouTube sign-in. Please send your link again."* — the only case where resending is asked for,
  because the held request did not survive the restart. The restart is logged with the reason.

### Risks, stated before building

- **Accounts can be flagged.** Using a real Google account's session for automated playback from a
  datacenter address is exactly what Google's abuse systems look for. yt-dlp's documentation warns that
  accounts used this way can be restricted. The portal page and `h connect youtube` must recommend a
  separate Google account made for the bot, not anyone's personal one, and say why.
- **Automated sign-in may simply be refused.** That is why the import page is part of this phase and not
  a later nice-to-have.
- **arm64 has no Chrome**, so browser sign-in and the keep-alive cannot run on a Raspberry Pi. There the
  import page is the only route, and without a live browser the imported session will go stale on
  Google's schedule. `li yt` on arm64 must say so rather than offering a sign-in that cannot work.
- **Sessions tied to an address.** A cookie exported from a browser at home and imported on a VPS is
  sometimes invalidated because Google sees the same session from two very different places. Browser
  sign-in on the bot's own host avoids this, which is another reason it is the primary path.

### Relationship to earlier decisions

- The migration's deletion of `cookies.txt` from old TTMediaBot backups **stays**. Those files are stale,
  sit in the wrong place, and nothing refreshes them — every reason given for deleting them still holds.
  The new file lives at `data/youtube_auth/cookies.txt`, is created only by this flow, and is refreshed.
- Create-bot's guarantee that no `cookies.txt` is placed in a new bot's folder **stays**. A new bot starts
  signed out and signs in through `li yt`.
- The OAuth device-code code paths are removed from playback. Whether `yl` survives for anything else is
  decided during implementation; if nothing needs it, it goes, rather than lingering as a sign-in that
  succeeds and then plays nothing.
- **Per-bot isolation is unchanged and binding.** Each bot's cookies, profile and refresh thread are its
  own. The bridge still validates `bot_id` and joins under `BOTS_ROOT` for every cookie read, and
  Duplicate Bot must not copy `data/youtube_auth/` or `data/browser/yt/`.

### What Phase 9 built, and where it departed from the above

Recorded 13 September 2026, when the code was written. None of it has yet run against live Google.

- **The pages were reviewed for accessibility before they were written**, and the review changed three
  decisions. The number Google shows for number matching is a readonly input that is **not** spelled
  out, because the phone's own screen reader says "eighty-eight", and a portal reading "8, 8" would not
  match it. The import page has a **file picker before the paste box**, because choosing a download is
  far easier with a screen reader than select-all, switch window, paste. Pasted cookies are **never
  echoed back** on an error; SC 3.3.7 exempts credentials, and Chrome spellcheck and Grammarly are
  switched off on that field because both send its text to a server.
- **`yl` survives, reduced**, to status and sign-out. Starting a sign-in from `yl` points to `li yt`.
- **The Google password is not stored.** Other services keep theirs so they can sign in again. A dead
  Google session needs the person present for the phone prompt anyway, so a stored password could not
  sign it in again alone.
- **No bot-restart path was built.** Nothing so far needs one: the bridge rebuilds a single bot's
  session when that bot's cookie file changes. Build it only if a live test shows a restart is truly
  required.
- **Commands run on the main loop**, which the plan did not account for. A refused request therefore
  never waits there for a refresh. It gets the renewal message at once, the refresh runs in the
  background, and exactly one follow-up is sent.
- **The keeper's schedule counts from `checked_at` in `session.json`**, not from start-up, because bots
  restart on every update and a timer that restarts with them might never fire.
- **Imported sessions go into the bot's Chrome profile where there is one**, and YouTube is asked
  whether they are signed in before they are accepted. That lets the keep-alive take them over, and a
  dead file is refused at import rather than failing later.
- **There are two proof-of-origin tokens, not one, and they are bound differently.** Measured on the
  VPS on 2026-09-21 against freshly resolved URLs for three videos, with `kuhao` confirmed signed in
  (`ytcfg.LOGGED_IN: true`). The *player* token is session-bound — `visitorData` signed out, the
  `DATASYNC_ID` signed in — and is what gets a datacenter address past `LOGIN_REQUIRED`. The *GVS*
  token, the `&pot=` on the `videoplayback` URL that Google's CDN checks, is bound to the **video ID**.
  The bridge was putting the session-bound token in both places, and a token with the wrong binding is
  not rejected — it is ignored. So the URL behaved exactly as an un-attested one: the first ~1,024,000
  bytes served, a flat 403 on everything past it, whatever offset or window size asked. Substituting a
  video-bound token into the same URL turned every one of those 403s into a 206, including an
  open-ended range from the middle of the file which then served the whole remainder in one request.
  Fixed in [055].
  - This answers the question that stood here before — whether the binding wants the full `DATASYNC_ID`
    or only the part before `||`. Neither: for the URL token the DataSync ID is the wrong identity
    altogether, and trimmed and full behaved identically (both 403).
- **Unverified and worth checking first on the VPS:**
  - Google's sign-in selectors and challenge URLs, in `bot/services/web/youtube.py`.
  - Which binding the *session* token should use. It is still the DataSync ID, and playback now works,
    so nothing currently argues against it — but it was never isolated the way the URL token has been.
- **Duplicate Bot needed no change.** It already copies only `config.json`, so it never copied
  `youtube_auth/` or `browser/`.
- **`h connect youtube` does not exist**, although README mentions `h connect`. The separate-account
  advice lives in the `li yt` reply and on every portal page instead.

### Found in the same log, not part of this phase

- **Apple Music now gets past launch** — [016] worked, Chrome starts — and fails one step later with
  "The Apple sign-in form did not appear". That is the adapter not finding Apple's sign-in iframe, which
  is issue #2 and belongs there.
- **An abandoned Spotify sign-in restarts go-librespot every hour, forever**:
  `failed exchanging device code: context deadline exceeded`. The daemon waits for a device code nobody
  entered, times out, exits, and the supervisor starts it waiting again. Unused device auth should stop
  until someone runs `li sp`, not loop.

### Phase 9 left two surfaces still describing the device code

Found afterwards, in `streamerbot.sh`, and corrected in [038] and [039]. Both are the same class of
defect: the *behaviour* moved in Phase 9 and the *messages about it* did not, so the script confidently
directed people to a flow that had been removed.

**The restore migration deleted the one thing worth keeping.** `migrate_one_bot` removed a restored
`cookies.txt` and printed "This version signs in with a code instead. Send li yt to the bot once it is
running." Both halves were wrong by then. The file is a YouTube sign-in in exactly the format this
version imports, and there is no code to send. Note that this plan asked for the opposite from the
start — "retains the old `cookies.txt` as an importable session", under "Legacy backup migration" — so
this is the implementation catching up with the plan, with the device-code clause dropped. Phase 2 is
where it diverged: the file genuinely was dead weight for one phase, and the deletion outlived the
reason for it.

**Why it is staged rather than written to `youtube_auth/cookies.txt` directly.** That file and the
bot's own Chrome profile have to agree. The keep-alive asks Chrome whether YouTube still considers it
signed in, and a session present only in the file answers no — so writing it straight through would
mark a working restored session "expired" on its first scheduled refresh, which is a worse failure than
deleting it, because it looks like Google ended the session. It goes to
`youtube_auth/imported_cookies.txt` and `YouTubeSessionKeeper.adopt_pending_import` runs it through
`import_text`, the same path as a session pasted into the portal, which loads it into the profile
first and already handles arm64 having no Chrome.

**The port-conflict note told people to look in the wrong place.** `PORT_CONFLICT.txt` and the lines
printed beside it said "YouTube and Spotify sign-in are unaffected: those use a code in the channel
rather than the portal." YouTube's sign-in is a portal page now, so a portal that cannot bind does stop
it. Spotify's claim is still true, and keeping the two apart is the whole value of the sentence.

### `auth_portal.host` was a setting the bot told you to edit by hand

Also [039]. `bot/modules/public_address.py::reachability_warning` detects that the portal is bound to
loopback while the link points elsewhere, and prints "Set `auth_portal.host` to 0.0.0.0 in this bot's
config.json and restart it." That is correct advice and it asks a blind user to hand-edit JSON over SSH
on a headless box — for the *common* deployment, since these bots run on a VPS and the person connecting
an account is at their own computer.

Creating a bot and Bulk Update Configuration now both ask, through one shared `ask_portal_host`. Two
copies of the question would drift, and the half most likely to be dropped from a copy is the sentence
saying what it costs: the portal has no login page by design, so it is guarded by unguessable links over
plain HTTP, and opening it to the network puts those links and what is typed into them on that network.
The default stays loopback — pressing Enter must never expose a portal — and choosing to open it repeats
the firewall point, because a free port is only half of reachability.

---

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
| **9** 🔨 | YouTube browser-session sign-in with scheduled per-bot refresh, replacing OAuth for playback (see "Phase 9 — YouTube sign-in through a real browser session") | On the VPS that currently answers `LOGIN_REQUIRED` to every client, a bot signed in through the portal plays a YouTube video and a livestream; a session killed on Google's side produces the renewal message to the requester, not "service unavailable"; two bots on one host hold two different Google sessions. **BUILT, NOT YET VERIFIED LIVE** ([027]) — see "What Phase 9 built" below; the exit criterion needs the VPS |

| **10** 🔨 | Adopt bot folders copied into `bots/` by hand, and decide foreign lineage by shape in the shell as well as in Python (see "Phase 10 — adopting a folder somebody copied in") | On a host with Docker, a TTMediaBot folder copied into `bots/` over scp is found by the scan, reported with its own nickname and server, adopted, and the resulting bot joins its channel under the name it had before. **BUILT, VERIFIED IN CI, NOT YET RUN AGAINST A REAL DOCKER HOST** ([029], [030]) — every `docker` call in the tests is a stub, so container creation is pinned by its flags rather than by a container existing |

| **11** 🔨 | Search without stopping playback, list every result with its kind, and answer `sv` from live state instead of a startup string (see "Phase 11 — searching while playing, and telling the truth about a service") | With a track playing on Apple Music, `sr` then `p <query>` lists twenty-five results naming each one's kind and the track keeps playing; `sl N` switches to the chosen one; `sv am` says Apple Music is connected and ready, `sv am h` explains it, and `sv nf` names `li nf`. **BUILT, NOT YET VERIFIED LIVE** ([031] to [037]) — the page split and the readiness lookup need a host with Chrome and a connected account |

| **13** 🔨 | YouTube leaves through an address YouTube trusts: Cloudflare WARP, a gluetun VPN or the user's own proxy, chosen from a menu, with a way to switch a blocked address (see "Phase 13 — YouTube refuses this server's address") | On a host whose own address YouTube refuses, `streamerbot.sh` option 9 then WARP makes `p <youtube url>` play, and the menu's test reports 3 of 3 videos played |

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

---

## Phase 10 — adopting a folder somebody copied in

Not in the original plan. It came from a user describing what they had actually done: taken a bot
folder from the old TTMediaBot, copied it into this project's `bots/`, and expected the manager to pick
it up.

### The failure was that nothing happened

Backup and Restore is the supported route and it works. But copying the folder straight in is the
obvious thing to do when the folder is right there, and it produced no error, no log line and no bot.
Every menu item in `streamerbot.sh` enumerates work from
`docker ps -a -f label=role=streamerbot`, so a directory with no container is absent from all of them.
Start All starts nothing extra, the bot list does not show it, the configuration check never reads it.
No code ran for that folder at all, which is why nothing could report on it.

This is worth recording as a shape, not just an incident: **anything keyed off the container list is
blind to a bot directory that has no container.** The same blindness would hide a bot whose container
was removed by hand, or one whose creation failed halfway through an earlier run.

The consequence for the design is that a scan is not enough on its own — the manager also says on
startup that such folders exist. A feature nobody knows to look for does not fix an invisible failure.
It only moves where the silence is.

### What it does

One menu item, Manage Bots → "Adopt Bot Folders Copied Into bots/":

1. Scan `bots/` for directories with no container.
2. Report each one — nickname, server, whether it came from an older version or another fork — with
   nothing written yet.
3. Ask once, for the whole batch.
4. Migrate, check the configuration, create the container, start it.

Reporting on every candidate before asking anything is deliberate. Asking folder by folder interleaves
a question with a report, and the answer then scrolls away from the thing it was about — the same
reasoning behind the one-message-at-start-and-finish rule for chat.

### It calls `migrate_one_bot`, it does not reimplement it

A copied folder and a restored one are the same problem arriving by different routes. Two copies of the
migration rules drifting apart is exactly what produced a `config.json` declaring `config_version` 2
against a `ConfigManager` that understood 1, which stopped every newly created bot before it reached
TeamTalk. So adoption reuses the restore path's migration wholesale, and the only new code is the
scanning, the reporting and the container creation.

The per-bot ports matter here for the same reason they do on restore, and slightly more sharply: a
copied folder arrives holding the ports it had **on the machine it came from**, which is a machine
where they were free. Dropped next to a running bot they collide silently, in the three ways recorded
under "Per-bot isolation".

### Three things it refuses rather than guesses at

- **A folder name that cannot be a bot name.** The name becomes the container name *and* the `bot_id`,
  and `bot_id` is the containment boundary that stops one bot reaching another's YouTube session. The
  pressure here runs the wrong way by default: the obvious fix for "my folder is called `My Bot`" is to
  loosen the validation. It must not be. The folder gets renamed, or it is not adopted. A space is by
  far the most common cause, since these folders usually arrive from a Windows machine, so the message
  names spaces specifically instead of only restating the rule.
- **A `config.json` that is not valid JSON.** `jq` would otherwise fail partway through the migration
  and leave the folder half converted, which is worse than not starting.
- **Several `config.json` files in one folder.** Choosing one is a guess about which bot the user
  meant, and getting it wrong brings up a bot under somebody else's identity.

### Whole installations get copied, not just data folders

People copy the entire old install directory, so `config.json` sits among the source tree rather than
at the top. That case is handled by lifting **named** files up — config, cache, log, and the credential
directories — never by moving everything. Hoisting the lot would put a second copy of the bot's own
source into `bots/<name>/`, which is then mounted over the container's data directory.

### A stopped container is not an orphan

The scan uses `docker ps -a`, so a bot that exists but is stopped is never a candidate. Treating it as
one would destroy and recreate a container somebody had deliberately stopped. This is the same
distinction the port allocator draws between "no bot owns this" and "this bot owns it and is using it",
and it is easy to get wrong in the same direction.

### Two bugs found while building it

Both let a configuration from another fork through while looking fine, and both are in the shell's half
of a rule Python already had right.

- **Lineage was judged by filename.** `bot_dir_is_legacy` looked for TTMediaBot's cache and log names,
  so a fork that had renamed those but still carried `services.vk` was reported to the user as *already
  current*. `bot/migrators/config_migrator.py` has always decided lineage by shape, for the reason
  recorded there — a fork that reached its own version 2 means something entirely different by it — and
  the shell now decides it the same way. The general rule: **a version number is only comparable within
  one lineage**, so anything that compares one must establish lineage first.
- **`services.default_service` was never migrated by the shell.** A restored or copied bot kept `"vk"`,
  which `ServiceManager` looks up in a plain dict — so the bot died during startup with a `KeyError`
  traceback rather than anything a user could act on. The bot's own migration repaired this, but only
  on the next start and only in memory until the config was rewritten, so the failure was survivable
  and therefore easy to leave in place. It is now fixed on disk when the migration runs, and named out
  loud, because it changes which service a bare search uses.

### One thing the smoke test caught that the unit tests would not have

`tools/check_config.py` takes the bot name it reports from the **parent directory of the config file**.
Mounted at a fixed `/bot`, every bot was reported as a bot called `bot` — harmless with one folder,
useless in a run adopting several. Each bot is now mounted at `/bots/<name>:ro`. Read-only for the
usual reason: a running bot holds a lock on its own `config.json`, and a check must never write to what
it is inspecting.

This is an argument for driving the whole flow once end to end, not only its parts. Every individual
function was correct; the defect was in what the composition printed.

### Verification, and what is still unverified

Tests run the real shell functions against real `bash` and `jq` rather than asserting that a line
appears in the script, because the bug being prevented is in what the code does to a folder. 43 tests
in `tests/deployment/test_adopt_copied_bots.py`, all passing on the CI host runner; the full deployment
suite is 153. They skip inside the image, like the rest of `tests/deployment/`, because `.dockerignore`
keeps `streamerbot.sh` out of it by design.

**Every `docker` call in those tests is a stub.** Container creation is pinned by its flags — the label,
`--network host`, `--restart always`, the data mount — so an adopted bot is the same kind of container
as one from Create Bot and does not drop out of the menus that filter on the label. What is *not*
tested is that the container then exists, starts, and connects. That needs a host with Docker, and it
is the exit criterion in the delivery table.

The local development host for this phase had neither Docker nor `jq`, so the behaviour tests silently
skipped there and the first full run was in WSL. Worth knowing for anyone working on the shell scripts
from Windows: **a green run on that host may mean the tests did not execute.** Check the skip count.

---

## Phase 11 — searching while playing, and telling the truth about a service

Not in the original plan. It came from a session log (`sv`/`sr`/`p` against Apple Music and YouTube,
15–16 September 2026) in which three separate things were wrong at once, and all three read to the user
as "the bot is broken" rather than as the three unrelated defects they are.

### A search stopped the music, and nothing said so

`BrowserEngine` keyed its pages by service alone — `self._pages[service]` — so every adapter call for a
service ran on the **same tab**. That tab is where the audio is. `AppleMusicAdapter.search` opens
`music.apple.com/<storefront>/search?term=…` with `page.goto(...)`, and navigating a tab ends whatever
MusicKit was playing in it. `is_logged_in` does the same thing: it `goto`s the home page to ask MusicKit
whether it is authorized. So *asking whether a service is signed in* could stop the music as surely as
searching did.

Nothing reported it because nothing failed. No exception, no `ServiceError`, no end-of-file event — the
tab simply became a different page, and `Player.state` still said Playing. This is the same shape as the
stream-proxy 403 recorded in Phase 9's notes: a failure with no error object attached is invisible to
every layer above it, and shows up only as behaviour a user has to describe in prose.

**One page is not enough. Each browser service gets a player page and an auxiliary page**, both in the
same persistent context so they share the sign-in:

- The **player page** is used by `play`, `pause`, `resume`, `stop`, `seek`, `set_volume`,
  `get_position`, `get_duration`, the audio-track calls and `now_playing`. Nothing else may navigate it.
- The **auxiliary page** takes everything that navigates for its own reasons: `search`, `is_logged_in`,
  `login`, the profile calls, `export_session` and `import_session`.

Cookies live in the context, not the page, so a sign-in on the auxiliary page is a sign-in for the
player page. `sign_out` drops every page for the service, not just one, or the next call resurrects a
page belonging to a context that has been closed.

The rule to carry forward: **a page that is producing audio is a playback device, not a browser tab.**
Any new adapter method that calls `goto`, `reload` or `go_back` belongs on the auxiliary page unless
playback is the thing it is changing.

### Search results mode showed exactly one result

Two independent limits, both set to 1, and neither wrong on its own:

- `CommandProcessor.search_results_count` — what `slc` sets — defaulted to **1**.
- `services.yt.search_results` and `services.ytm.search_results` in the shipped config are **1**, and
  `YtService.search` uses that whenever `limit` is None.

In ordinary mode both are right: `p <query>` plays the best match, and asking YouTube for twenty-five
results to throw away twenty-four is waste. In search results mode they mean "here is your choice of
one", which is not a choice. The log shows it plainly — `limit=1 … results=1` for the same Apple Music
query that returned 25 a minute earlier with `limit=None`.

**Search results mode gets its own default, and does not read the play-the-top-hit limit.** `slc`
continues to set it, `slc 0` means "as many as the service returns", and `services.*.search_results`
keeps meaning what it means for a bare `p`.

### The list did not say what anything was

`BrowserService.describe_results` was built in Phase 6 to the plan's "Search result ordering" section —
summary of counts first, then numbered entries each naming its kind — and it has tests. It was never
called. `_search_and_play` formats the list itself as `f"{i + 1}: {track.name}"`, which drops the kind
entirely, so an album, an artist and a song are three identical-looking lines.

`order_results` *is* called, so the ordering was right and only the labelling was missing: the results
are already grouped, artists and albums and playlists before individual tracks, exactly as specified.

Wiring it up goes through the service rather than the command, because only the service knows what its
kinds are. `Service.describe_tracks(tracks)` returns a plain numbered list; `BrowserService` overrides it
to reuse `describe_results`. A service with no notion of kind — YouTube, Spotify — keeps the plain list
rather than inventing a label for everything.

### Playback continues until a track is chosen

This is the point of the mode and it was never written down. `p <query>` in search results mode queues
nothing and plays nothing; it reads out a list. **Whatever was playing keeps playing** until `sl N`
selects from that list, and only then does the player change tracks. With the page split above this is
now what happens for the browser services too, which is where it visibly was not.

### `sv <service>` called a service "not ready" while it was playing

`BrowserService.initialize()` sets `warning_message` to "%(service)s is not ready yet." when no engine is
attached yet — correct at that moment, because `ServiceManager` is built before the engine exists. But
`attach_engine()` never cleared it. Nothing else ever wrote to that field, so the warning outlived the
condition for the whole life of the process, and `sv am` said Apple Music was not ready in the same
minute Apple Music was streaming into the channel. `NetflixService` and `SpotifyService` have the same
line and the same omission.

**Readiness is a question answered when it is asked, not a string left behind by startup.** `sv` now
reports, per service:

- **Disabled**, with the reason, when `is_enabled` is false. This is the arm64-has-no-Chrome case and it
  is already set correctly during startup.
- **Not connected**, naming the command that fixes it, when the service needs an account and the auth
  portal says none is connected.
- **Connected and ready** otherwise.

The sign-in state comes from `AuthPortal.statuses()`, which is what `li` already reports and is a cheap
local lookup. It is deliberately **not** `engine.is_logged_in()`: that is a browser round trip of several
seconds on a command that has always answered instantly, and — before the page split above — it would
have stopped the music to answer. If the stored account has since been signed out on the service's side,
the next `p` says so through `NotSignedInError`, which already names the command.

`ytm` reports YouTube's state, because it plays through the same session; it is not a separate account.

### `sv <service> h` had nothing to say

Every service sets `self.help = ""` in its constructor and nothing ever sets it to anything, so
`sv am h`, `sv yt h` and `sv az h` all answered "This service has no additional help" — while the `sv`
listing was actively telling users to send exactly that command.

Each service now carries its own help: what it plays, what it needs, what it cannot do, and the command
that connects it, with the live status line included so the answer is about *this* bot rather than about
the service in general. This is the per-service walkthrough the plan's "Help must explain how to connect
each service" section describes; `h connect <service>`, when it is built, should call the same text
rather than write a second copy of it.

### Spotify and Amazon Music got the ordering the plan already specified

The plan's "Search result ordering" section names Spotify, Apple Music and Amazon
Music. Only the browser services were ever built to it, and Spotify's search asked
`/search` for `type=track` alone — so however well the list labelled its results,
there were never any albums or artists in it to label.

Both are done now, and both needed the same thing to be safe: **a top result.**
Ordering containers before tracks is right for a list being heard, and it is wrong
for `p QUERY`, which plays the first result. Apple Music escapes this because its
own search page supplies a `top`; Spotify has no such concept and Amazon's scrape
is in DOM order, so a bare search on either would have started an album — or an
artist, which cannot be played at all. Both now promote their first real song, so
the containers lead the list without the bare command losing the song.

Selecting a container has to expand it. Spotify's `get()` already turned an album,
artist or playlist link into tracks, so `sl` routes through it rather than handing
the daemon a URI it cannot play; the browser services need none of this, because
the site's own player queues an album when it is given one.

Two defects in the Amazon search found on the way, both of the shape Apple Music
had already been fixed for: the query was interpolated raw into a path segment, so
`AC/DC` asked for a different page entirely, and the scrape waited a flat 3.5
seconds rather than until the list stopped changing. **The lesson from Apple Music
did not travel to the file next to it** — worth knowing when the third adapter
grows a search.

`bot/services/results.py` now holds the ordering, the labels and the top-result
rule. They lived in `browser_service.py` while the four browser services were the
only users; Netflix borrowed them, then Spotify, and a third borrower is the point
at which shared vocabulary stops being a detail of one module.

### Found while building it, not part of the phase

`tools/compile_locales.py` — the documented way to regenerate the catalogs after
changing a string — could not run on a checkout whose path contains a space. It
built one interpolated string per babel command and ran it with `shell=True`, so
the shell split the path, babel was handed half a directory name, and the script
reported "Bable is not installed" for a babel that was installed and working. The
commands are argument lists now. Worth recording because the failure named the
wrong cause: anyone hitting it would go and install a package they already had.

It also wrote **absolute** paths into every `#:` source reference. Catalogs
generated inside the image said `/work/bot/__init__.py`; running the documented
command on a developer's machine rewrote all 390 of them to that machine's path,
which is an unreadable diff and a local path committed to the repository. Paths
are relative now and babel runs from the repository root, so the output is the
same wherever it is generated. **Generated files have to be reproducible or they
cannot be reviewed**, and the only thing that made this visible was reading the
diff rather than the test result.

### Exit criterion

On a host with Chrome and a connected Apple Music account: with a track playing, `sr` then `p <query>`
returns a numbered list of twenty-five results naming each one's kind, **and the track is still
playing**; `sl` and a number switches to the chosen one. `sv am` answers "Apple Music is connected and
ready", `sv am h` explains Apple Music and how it is connected, and `sv nf` on the same host says
Netflix is not connected and names `li nf`.

## Phase 12 — letting the portal port through ufw

`assign_unique_bot_ports` gives each bot a free portal port, but a free port is only half of reachability:
with ufw on, the portal binds and the link still does not open. `PORT_CONFLICT.txt` and `ask_portal_host`
already told the user to allow the port and never helped. Manage Bots option 15 and `--firewall` now do
(`ufw_sync_portal_ports` in `streamerbot.sh`, changelog [040]).

- **Only the portal port is opened.** go-librespot's API port and the stream relay are loopback-only.
- **Rules are tagged** `StreamerBot portal <bot>` so a rule left by a port change is recognised and
  offered for removal, and nobody else's rule is ever touched.
- **ufw is never enabled from here** — enabling on a remote host can cut off the SSH session.
- **Loopback or disabled portals are reported and skipped**; opening their port does nothing.
- **Automatic pass** after create, restore, Start All and Restart All: adds missing rules only, only when
  ufw is already active, never deletes.
- Found on the way: `jq '.enabled // true'` reads `false` as absent, so a disabled portal looked enabled.
- **Unverified:** run against a stub ufw only (`tests/deployment/test_ufw_firewall.py`). The real
  `ufw status` comment format is assumed from 0.36 and has not been observed on a live host — the session
  was not root. Also, `test_port_allocation.ExhaustionTests.test_the_warning_names_what_still_works`
  already failed before this phase (asserts wording Phase 9 changed).

---

## Phase 13 — YouTube refuses this server's address

kuhao could search YouTube and play nothing. Search worked, every stream resolve failed with
`LOGIN_REQUIRED: Sign in to confirm you're not a bot` on every client, signed in or not. About 24 bots
on the same OVH host (AS16276) showed the identical failure, 700-990 lines each in the recent log.

### What it was not

- **Not the sign-in.** kuhao's session was healthy: imported, 27 cookies, a DataSync ID,
  `needs_sign_in: False`, refreshed on schedule. Signed in, it still failed.
- **Not the token binding.** The stored DataSync ID is `115949595150057545456||`, and a token bound to
  the raw value looked like an obvious mistake. A probe inside the bridge container with the raw and the
  bare ID gave `LOGIN_REQUIRED` both ways, so `contentBindingFor` was left alone. **The existing test that
  pins the raw `account||` value proves nothing about YouTube accepting it.**
- **Not total.** From OVH an old video resolved and played while two recent ones were refused, so the
  address is distrusted, not banned. That is what made a first probe misleading (below).

### What was built (changelog [043], [044])

- `youtube_egress.sh`, sourced by `streamerbot.sh`: menu option 9 and `--youtube-egress`,
  `--check-youtube-egress`, `--rotate-youtube-ip`. Modes: direct, **Cloudflare WARP**, **gluetun VPN**,
  the user's own proxy. Plain numbered steps for someone with no VPN account.
- **The proxy reaches both the bridge and the stream relay.** The bridge uses Node's
  `NODE_USE_ENV_PROXY` (no new dependency; the POT provider stays direct via `NO_PROXY`); the relay passes
  `proxies=` to `requests`. A resolved `videoplayback` URL is signed for the address that resolved it, so
  resolving through a proxy and fetching from the host's own address would 403. Only hosts that are
  `googlevideo.com`/`youtube.com` or a subdomain are proxied; a bare suffix match let `evilgooglevideo.com`
  through, and a test caught it.
- **The proxy listens on Docker's bridge gateway address only.** The bridge container is on the default
  bridge and bots are on the host network, so loopback reaches neither and `0.0.0.0` would be an open proxy
  on the public address.
- **Settings are untracked** (`youtube_proxy.env`, `youtube_vpn.env`, `youtube_egress_ips.log`, 0600):
  a proxy URL carries credentials and `project.env` is committed.
- `/live/`, `/embed/` and `/v/` URLs are recognised (`extractVideoId` moved into `media.mjs`, so it is
  unit-tested). About fifty `/live/` attempts had died as "Invalid YouTube URL". A URL the bridge cannot
  read is no longer retried.

### Measured, not assumed

- **One video is not a test.** The probe plays three (a majority must) and invalidates the bridge's
  hour-long resolve cache first, or a cached success passes a blocked address. It also fetches a few
  bytes of the stream through the same proxy, and follows googlevideo's `302`.
- **WARP's port answers SOCKS5 and plain HTTP proxy requests on the same port.** That mattered: Node's env
  proxy and `requests` speak HTTP proxies only, and it was remembered as SOCKS-only.
- **Through WARP, 3 of 3 videos played**, including the two OVH direct refuses.
- **WARP's address is not reliably switchable.** A fresh registration gave the same address once and a
  different one the next time. Rotation therefore tries twice and then says which thing happened (same
  address again, or a new one YouTube also refused) rather than claiming a switch.
- Rotation never accepts an address already in the log of the last 20, and proves each new one with the
  probe. For a VPN, gluetun picks a server at random each start, so a restart usually changes it.

### Unverified

- **A paid VPN provider** was never started: it needs an account. The gluetun launch follows its
  documented settings and the image tag exists, nothing more.
- The provider steps in the "I need a VPN account" text follow gluetun's provider list and have not been
  checked against its wiki.
- WARP addresses are shared and can be flagged by YouTube; passing today does not promise tomorrow. A
  residential proxy is the most reliable option and was not tried.
- Bots and the shared bridge must be recreated to read `YOUTUBE_PROXY_URL` (the menu does this and stops
  them briefly); it has not been applied to the live bots.
- The pre-commit hook in `.githooks/` was skipped on the commit because it is not executable.

### ExpressVPN over gluetun did not connect (measured 2026-09-20)

- gluetun looped `TLS key negotiation failed to occur within 60 seconds` against a new ExpressVPN server
  every ~75 s, with no `AUTH_FAILED`. OpenVPN checks the login only after the server answers, so the
  credentials were never judged; it was not a password problem.
- A packet capture on the host showed the handshake leaving `eno1` and no reply returning. ufw was not
  the cause (outgoing allowed). Either the host's network drops the replies or ExpressVPN ignores
  hosting-company addresses; which of the two is **not determined**.
- TCP is not an escape for ExpressVPN: all 171 servers in gluetun's list are UDP-only, and TCP 1195 and 995
  were refused on a 12-server sample. TCP 80/443 answered HTTP 404/400 (a web front end, not OpenVPN).
  A raw UDP probe proves nothing because ExpressVPN uses tls-crypt and ignores unauthenticated packets.
- Built: `egress_vpn_explain_failure` names silent-server versus rejected-login, a UDP-to-TCP retry for
  providers other than ExpressVPN, a Proton free-servers prompt (`FREE_ONLY=on`), Proton WireGuard help
  text, and a failed setup now resets the saved mode to direct instead of leaving a dead proxy saved.
- **Measured 2026-09-21: ProtonVPN free, WireGuard.** Connected in about 10 s, so this host carries VPN UDP
  fine and the ExpressVPN failure was on ExpressVPN's side. But YouTube refused every free address: six
  different exits (Atlanta 89.187.171.228, 212.104.215.152, 149.40.62.15, 149.22.84.167, 185.45.15.37)
  played 0 or 1 of 3 videos. Free-plan addresses are datacenter addresses YouTube already blocks. Bots were
  not recreated; the bridge was put back to direct. A different result needs a residential proxy
  (`url` mode), not another datacenter VPN.
