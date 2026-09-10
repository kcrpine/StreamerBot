# StreamerBot

An accessible streaming bot for TeamTalk 5. It plays YouTube, Spotify, Netflix,
Disney+, Apple Music, Amazon Music and direct stream URLs into a voice channel,
and it can play films and shows with the **audio description** track where the
service offers one. One Docker container per bot, many bots per machine, managed
from a command-line tool written to be used with a screen reader.

> **Before you use the video services, read this.** Rebroadcasting Netflix,
> Disney+, Apple Music or Amazon Music into a TeamTalk channel is very likely a
> breach of those services' terms of service, and may be a copyright problem in your
> country. That is your decision to make, not something this project can make for
> you. YouTube, Spotify and direct URLs carry no such problem for ordinary use.

## This is a fork

StreamerBot is a fork of [TTMediaBot](https://github.com/gumerov-amir/TTMediaBot)
by Gumerov Amir Eduardovich, taken from
[JoaoDEVWHADS/TTMediaBot](https://github.com/JoaoDEVWHADS/TTMediaBot) and
substantially rewritten. It is MIT licensed, and the original copyright notice is
retained in [LICENSE](LICENSE).

If you want the original project, use the links above. This one has diverged
considerably.

## What this fork adds

**Five more services.** The original played YouTube and YouTube Music. This adds
Spotify, Netflix, Disney+, Apple Music and Amazon Music. They do not all work the
same way, and the table below is worth reading before you pick one.

**Audio description.** Films and shows can play with the described audio track
where one exists. The bot asks first, and remembers the answer if you tell it to.
For a blind listener this is the difference between a film being watchable and
being ninety minutes of unexplained silence.

**No more `cookies.txt`.** YouTube used to need a cookie file exported from a
desktop browser every few weeks. It now signs in with a short code you enter on
any device, and refreshes itself from then on. Spotify pairs the same way.

**Accounts connect through a web page, not the chat.** A password does not belong
in a channel where everyone can read it. Two-factor codes have their own step, so
a code prompt no longer dead-ends a sign-in.

**Each bot's accounts are its own.** Two bots on one machine never share a login.

**Apple Music downloads.** `dl` on an Apple Music link uploads the audio to the
channel: a single track as one file, an album or playlist as one zip.

**A manager built for screen readers.** No screen clearing, no self-rewriting
progress lines, destructive actions confirmed by typing a word rather than `y/N`,
and flags for skipping the menus entirely.

Full detail is in [CHANGELOG.md](CHANGELOG.md).

## What each service needs

| Service | Plays through | Needs |
| --- | --- | --- |
| YouTube, YouTube Music | mpv | Nothing. Signing in only adds age-restricted content |
| Spotify | go-librespot | Premium. A Spotify application if you want to search by name |
| Netflix, Disney+ | Chrome | An account. **amd64 only** |
| Apple Music, Amazon Music | Chrome | An account. **amd64 only** |

**The four browser services do not work on ARM,** including Raspberry Pi. Google
publishes no Chrome for linux/arm64, and only Chrome carries the Widevine module
those services need to decrypt anything. On ARM they disable themselves and say
so rather than failing when you ask for a title. YouTube, Spotify and direct URLs
work everywhere.

## Requirements

A Linux machine with `sudo`. Debian, Ubuntu, Fedora and Arch are all fine. Docker
is installed for you if it is missing.

You do **not** need to download the TeamTalk SDK, create a `TeamTalk_DLL`
directory, or find a `cookies.txt`. All of that is either handled inside the
Docker image or gone.

## Installing

### If you have nothing yet

This installs git and Docker if they are missing, downloads StreamerBot, and
starts the manager.

```bash
curl -fsSL https://raw.githubusercontent.com/kcrpine/StreamerBot/main/install_git_clone.sh -o install_git_clone.sh
```

```bash
less install_git_clone.sh
```

```bash
sudo bash install_git_clone.sh
```

The middle step is deliberate. That script runs as root, and reading it first is
the reasonable thing to do with anything you are about to give root to. There is
no one-line version on purpose.

It finishes by opening the manager, so the first run continues straight into
creating a bot. Running it again on a machine that already has StreamerBot is
safe.

### If you already have git and Docker

```bash
git clone https://github.com/kcrpine/StreamerBot.git
```

```bash
cd StreamerBot && ./streamerbot.sh
```

The manager asks for `sudo` when it needs it. On first run it asks once for the
GitHub username to take updates from, and writes the answer to `project.env`.

## Using it

Send `h` to the bot in TeamTalk for the command list, and `h connect` for how to
connect each account.

To connect an account:

- **YouTube:** send `li yt`. The bot replies with a code and an address. Enter the
  code there, and it picks it up on its own.
- **Spotify:** send `li sp`, then enter the code at `spotify.com/pair`. Playback
  needs Premium. Searching by name additionally needs a Spotify application; `h
  connect sp` walks through it, and a pasted Spotify link works without one.
- **Netflix, Disney+, Apple Music, Amazon Music:** send `li` and follow the link
  it gives you. The password is typed on that page, never in the channel.

`li` on its own says what is connected. `da on` turns audio description on for
good, `da off` turns it off, and `da` alone asks each time. `pf` lists and picks
streaming profiles, which matters on Netflix and Disney+ because each profile has
its own watchlist.

## Configuring

[project.env](project.env) holds the settings shared by the whole installation:
which GitHub repository updates come from, the Docker image name, the pinned
TeamTalk SDK and go-librespot versions, and how often to check for updates.

Each bot has its own `bots/<name>/config.json` with its TeamTalk details, its
services and its startup commands. `start_commands` is how a bot plays a stream
the moment it connects, and the manager asks for one when you create a bot.

Non-interactive flags, for skipping the menus:

```bash
./streamerbot.sh --status
```

`--services`, `--start-all`, `--stop-all`, `--restart-all`, `--check-updates`,
`--logs NAME` and `--help` all work the same way.

## Accessibility

Most of the people this is built for are blind, so accessibility is a
requirement here rather than a feature.

The web pages target **WCAG 2.2 AA**, and adopt SC 2.4.13 Focus Appearance
deliberately. Every step is a real page load so a screen reader announces the new
title; there are no modals anywhere; errors are server-rendered; and one-time
codes go in a single field that accepts paste, never six boxes that break it.

Chat messages are one message rather than several, because each one is its own
announcement. Codes are read out character by character, since a synthesiser will
otherwise try to pronounce `BCDF-GHJK` as a word.

The command-line manager does not clear the screen, does not print progress bars
that rewrite themselves, and asks for a typed word before anything destructive.

This is not decoration. During development a review of the sign-in pages
overturned five decisions that had already been settled, the worst of which would
have made the YouTube code impossible for a screen reader user to copy — for
exactly the people who most need to copy it rather than transcribe nine
characters by ear.

## Contributing

**Anyone can open an issue or a pull request.** No permission is needed, and this
is the normal route. Pull requests run the full checks automatically — the test
suite, the shell scripts, and a Docker image build — and a green run is what gets
one merged.

**To report a fault,** use the [bug report
form](https://github.com/kcrpine/StreamerBot/issues/new?template=bug_report.yml).
Only two of its questions are required, so a report is worth filing even when you
cannot answer the rest. It asks whether you have a log file and where to find one:
each bot writes its own at `bots/<your bot>/StreamerBot.log`. If none of the forms
fit, open a blank issue — it gets sorted automatically.

**To become a collaborator with write access,** use the [collaboration request
form](https://github.com/kcrpine/StreamerBot/issues/new?template=collab_request.yml),
or say so in a pull request. Access is granted by invitation from the repository
owner; it is not something a fork or a pull request can give itself.

Issues are public, including anything written in them. Do not paste passwords, the
contents of a bot's `secrets` folder, or an account portal link — a portal link is
itself a credential. A normal `StreamerBot.log` is safe to share: the bot strips
passwords and tokens from its own log on purpose.

### If you use Claude Code

The [.claude](.claude) directory is committed on purpose. It holds the full
implementation plan, the accessibility hooks, and the settings, so you start from
the same plan and the same rules rather than reconstructing them.
[CLAUDE.md](CLAUDE.md) is the orientation.

Run this once per clone:

```bash
bash tools/install-hooks.sh
```

That installs the hooks and copies the plan into `~/.claude/plans`, without
overwriting one you already have. **Read the plan before starting**: it records
what each phase actually found, including several decisions that were later
proved wrong, none of which is recoverable from the code.

The accessibility agents the hooks call are referenced rather than bundled — they
belong to their own project and are better installed from source. Without them
the hooks simply print their reminder, so a fresh clone still works.

## Running the tests

The suite cannot run on a Windows host: it needs the TeamTalk SDK and libmpv, and
neither is in the repository. Run it in the image instead:

```bash
docker run --rm -v "$PWD:/work" -w /work --entrypoint bash streamerbot:test -c 'python -m unittest discover -s . -p "test_*.py"'
```

## License

MIT, retaining the upstream copyright. See [LICENSE](LICENSE).

One inconsistency inherited from upstream and worth stating rather than hiding:
`mpv.py` is a vendored copy of [python-mpv](https://github.com/jaseg/python-mpv),
which is **AGPLv3**. It sits inside an otherwise MIT-licensed project. If that
matters for how you intend to use this, look at it before you do.
