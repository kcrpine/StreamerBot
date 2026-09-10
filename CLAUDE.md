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

Two test files (`test_update_shared_youtube.py`, `tests/deployment/`) read the shell scripts from disk
and **skip** inside the image, because `.dockerignore` excludes those scripts by design. They pass on a
host with the scripts present, so `python -m unittest test_update_shared_youtube` works on Windows.

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
  what stops one bot reaching another's OAuth tokens. Do not relax it.
- The `/bots` mount must be **`:rw`**; tokens are written to `bots/<id>/youtube_auth/`. Both scripts
  check the mount's RW flag before deciding a container is current, because a read-only container is
  healthy and useless.
- The process installs `unhandledRejection`/`uncaughtException` handlers on purpose. The OAuth device
  flow polls Google in the background long after `/auth/start` returns, and Node makes an unhandled
  rejection fatal — one bot's abandoned sign-in previously killed YouTube for every bot on the host.

There is no `cookies.txt` anywhere any more. Sign-in is an OAuth device code (`yl` command,
`/auth/start`), and anonymous use still works exactly as the cookie-less path did.

**Two things about resolving a stream that cost a day each.** Both are in `media.mjs`'s
`planPlaybackAttempts`, which is where the client chain lives and is pure so it can be tested:

- **`ClientType` is not the vocabulary `getBasicInfo({client})` speaks.** `ClientType` is for
  `Innertube.create({client_type})`. `getBasicInfo`'s `client` option is validated against
  `Constants.SUPPORTED_CLIENTS`, which holds the enum's **keys** (`'TV_EMBEDDED'`), while
  `ClientType.TV_EMBEDDED` is its **value** (`'TVHTML5_SIMPLY_EMBEDDED_PLAYER'`). Passing the enum
  throws `Invalid client: …` inside the process, so that fallback silently never ran. The two
  parameters look interchangeable and are not.
- **A signed-in session must be able to fall back to an anonymous one.** YouTube answers **400** to an
  OAuth-authenticated player request that it serves anonymously, so completing `yl` for age-restricted
  content otherwise breaks *all* playback. Authenticated attempts come first — they are the only ones
  that can return age-restricted or member content — and the anonymous session is built lazily. Search
  is unaffected either way, so "search works but nothing plays" is the signature of this class of bug.

A chain of fallbacks is only a fallback if the entries differ. The original read
`['YTMUSIC', 'MWEB', ClientType.TV_EMBEDDED]` and was one real client: the caller rewrote `'YTMUSIC'`
to MWEB and the third was invalid. `youtubei.js` is pinned to `#main`, so this is a moving target —
when playback breaks, probe which clients resolve *today* before changing the order.

### Per-bot isolation is a requirement, not an accident

Each container is created with `-v "${BOTS_ROOT}/${bot}:/home/streamer/StreamerBot/data"`, so every
credential path (`data/secrets/`, `data/youtube_auth/`, `data/browser/`, `data/librespot/`) resolves
inside that one bot's directory. **Nothing credential-related may live outside `data/`.** Two bots on
one host must never share a streaming account.

Bot directories are owned by uid 1000 (the container user). `update.sh` deliberately prunes `bots/`
from its repo-wide `chown`/`chmod` pass — a blanket `chmod -R 777` there once left credentials
world-writable and locked the container out.

**Bots run with `--network host`, so any port in `config.json` must be unique per bot.** Two are:
`auth_portal.port` (4419) and `services.sp.api_port` (3678). Both were written as the same constant
into every bot, so the first bot to start took them and every other bot lost its portal *and* its
Spotify daemon. Neither failure named itself — a portal that failed to bind was indistinguishable from
one switched off, so `li` blamed the configuration, and go-librespot's output went to `/dev/null` so its
restart loop logged no exit code. `streamerbot.sh` now allocates per bot (`assign_unique_bot_ports`) on
create, on restore, and automatically before Start All and Restart All, with `--repair-ports` for
existing bots.

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

Commit messages explain *why*, particularly where a choice looks odd. Several non-obvious decisions in
this codebase exist because the obvious version was tried and broke something.
