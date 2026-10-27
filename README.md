# StreamerBot

An accessible streaming bot for TeamTalk 5. It plays YouTube, Spotify, Netflix,
Disney+, Paramount Plus, Hulu, Tubi, Apple Music, Amazon Music, podcasts and
direct stream URLs into a voice channel, and it can play films and shows with the
**audio description** track where the service offers one. One Docker container
per bot, many bots per machine, managed from a command-line tool written to be
used with a screen reader.

> **Before you use the video services, read this.** Rebroadcasting Netflix,
> Disney+, Paramount Plus, Hulu, Tubi, Apple Music or Amazon Music into a TeamTalk
> channel is very likely a breach of those services' terms of service, and may be a
> copyright problem in your country. That is your decision to make, not something
> this project can make for you. YouTube, Spotify, podcasts and direct URLs carry no
> such problem for ordinary use.

## New major versions

The big changes in each major version, newest first. A major version is a new
first number: 2.0, 3.0 and so on. Every smaller change is in
[CHANGELOG.md](CHANGELOG.md), where a major version's heading ends with "New major
version!".

### StreamerBot 2.0 (12 October 2026)

- **Podcasts.** Send `pc` and a few words to search Apple Podcasts, fyyd,
  gpodder.net and Spotify at once, then `pc` and a number to open a show. `pe` and
  a number plays an episode, `pd` and a number downloads one to the channel, and
  `pd` on its own downloads the newest episodes as one zip file. `pcd` chooses
  which directory to search. Spotify episodes play but cannot be downloaded.
- **Podcast subscriptions.** `pa` subscribes to the open show. The bot sends you a
  private message when a new episode is out, or the next time you are online.
  `ps` lists your subscriptions and `pu` removes one.
- **Three more video services.** Paramount Plus (`sv pp`) and Hulu (`sv hu`) need
  an account, connected with `li pp` or `li hu`. Hulu streams only in the United
  States. Tubi (`sv tb`) is free with advertisements and needs no account.
- **Copyable links.** `li` sends each link as a message of its own, after the
  instructions, so copying the message copies only the link. It always includes a
  YouTube link, so a YouTube sign-in that has stopped working can be replaced.
- **Spotify search works again.** It had failed with an error since September.

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

**YouTube that plays from a server.** From most VPS addresses YouTube refuses to
play anything without a signed-in account, and it no longer plays for the TV code
sign-in either. The bot signs a Google account in through its own Chrome, keeps
that session alive by itself, and holds a request while it renews rather than
failing it. Where Google refuses the bot's browser, a session exported from your
own browser can be imported instead, by choosing the file on a web page. Nobody
re-exports a cookie file every few weeks. Spotify pairs with a code.

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
| YouTube, YouTube Music | mpv | Nothing where YouTube trusts the address. On most VPS hosts, a Google account made for the bot |
| Spotify | go-librespot | Premium. A Spotify application if you want to search by name |
| Netflix | Chrome | An account. **amd64 only** |
| Disney+, Paramount Plus | Chrome | An account, and on most VPS hosts a residential proxy (see below). **amd64 only** |
| Hulu | Chrome | An account, and a bot running in the United States. **amd64 only** |
| Tubi | Chrome | Nothing. **amd64 only** |
| Apple Music, Amazon Music | Chrome | An account. **amd64 only** |
| Podcasts | mpv, or go-librespot for Spotify shows | Nothing. Spotify shows need what Spotify needs |

**The browser services do not work on ARM,** including Raspberry Pi. Google
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

- **YouTube:** send `li yt` and follow the link. Use a Google account made for
  the bot, not your personal one: Google may restrict an account that plays
  videos automatically from a server. The page signs in through the bot's own
  browser, including Google's "check your phone" step. If Google refuses, or on
  ARM where there is no browser, the same page offers importing a session
  exported from your own browser. When Google ends the session, the bot says so
  in TeamTalk and `li yt` connects it again.
- **Spotify:** send `li sp`, then enter the code at `spotify.com/pair`. Playback
  needs Premium. Searching by name additionally needs a Spotify application; `h
  connect sp` walks through it, and a pasted Spotify link works without one.
- **Netflix, Disney+, Paramount Plus, Hulu, Apple Music, Amazon Music:** send `li`
  and follow the link it gives you. The password is typed on that page, never in
  the channel. Tubi needs no account. On a VPS, set up a residential proxy for Disney+ and Paramount Plus first, as described in "Disney+ and Paramount Plus on a server".

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

### Changing settings from TeamTalk

A bot admin can change most settings without logging in to the server. Send `cf` (or `config`) to the bot in a private message, and it answers with a numbered menu: server connection, bot identity, startup commands, playback, files and downloads, messages, services, and the account portal. Send a number to open a group, then a number to pick a setting, then the new value. Each change is saved as soon as you make it, so there is no need to send `sc`. Send 0 to go back, and to keep a value as it is.

Some settings, such as the server address or a startup command, take effect only after a restart. After changing one, every menu ends with "99. Restart now to apply changes". Send 99, and the bot restarts and tells you when it is back. If you changed the server details, it asks once more first, because a wrong address means the bot cannot reconnect, and then only `streamerbot.sh` on the server can fix it. Passwords are shown in the menu, and your TeamTalk client keeps them in its history.

### Languages

The bot speaks these languages in its messages and on its web pages:

- English (`en`), the default
- Arabic (`ar`)
- Brazilian Portuguese (`pt_BR`)
- Hungarian (`hu`)
- Indonesian (`id`)
- Russian (`ru`)
- Spanish (`es`)
- Turkish (`tr`)

To change the language, send `cf`, choose "Bot identity" and then "Language". The change applies at once and is saved. An admin can also send `cl` with the code, for example `cl es`, but that change lasts only until the bot restarts unless it is followed by `sc`.

Non-interactive flags, for skipping the menus:

```bash
./streamerbot.sh --status
```

`--services`, `--start-all`, `--stop-all`, `--restart-all`, `--check-updates`,
`--channel`, `--logs NAME` and `--help` all work the same way.

### Disney+ and Paramount Plus on a server

Disney+ and Paramount Plus refuse data-centre addresses, which is where most VPS servers are. Disney+ shows "Sorry, an unexpected error has occurred" instead of its sign-in form, and Paramount Plus answers "406 Not Acceptable". Your password never reaches the site, so it is not the password. Signing in on your own computer and copying the session over does not help either, because the Disney service that refuses the address is the same one that hands out the keys for staying signed in and playing.

The fix is a residential proxy: the bots' browser goes out through a home internet address instead of the server's. Only Disney+ and Paramount Plus use it unless you choose otherwise. Every other service keeps using the server's own address.

In `streamerbot.sh`, choose option 10, "Disney Plus and Paramount Plus Proxy". It explains which proxy to buy, tests the one you enter, and applies it to every bot on the server.

**Which kind to buy.** The bot's browser downloads the whole video, even though only the sound reaches TeamTalk, which is about 1 to 3 GB an hour. Buy a static residential proxy, also called an ISP proxy, sold per address with unlimited or large traffic. Avoid plans sold per gigabyte, where one film can cost 5 to 20 dollars. Pick an address in the same country as the streaming account. One address is enough for all the bots on a server. Choose the HTTP type with a username and password, because Chrome cannot sign in to a SOCKS5 proxy.

Providers with plans that fit, at their listed prices in October 2026 (check before buying; none has been tried with Disney+ yet):

| Provider | Plan | Listed price | Traffic | Notes |
| --- | --- | --- | --- | --- |
| [IPRoyal](https://iproyal.com/static-residential-proxies/) | Static residential | About $2.70 an address for 30 days | Unlimited | US and Canada. A 24 hour plan for about $1.80 lets you test first |
| [Proxy-Seller](https://proxy-seller.com/isp/) | ISP proxy | From about $1 an address a month | About 100 GB an address a month (fair use) | US, no Canada |
| [Webshare](https://www.webshare.io/static-residential-proxy) | Static residential | About $0.30 an address, smallest plan 20 addresses (about $6 a month) | From 250 GB a month (fair use) | Check at checkout whether the addresses are shared |

The setting lives in `browser_proxy.env` beside `streamerbot.sh`. It is never committed, and the updater leaves it alone.

### Choosing stable or latest updates

Each server picks where its updates come from:

- **Stable**, the default: released versions only. Choose this for bots people
  rely on.
- **Latest**: every change as soon as it is pushed, including work not yet
  released. Choose this to test new work or if you maintain StreamerBot.

To see which one a server uses:

```bash
./streamerbot.sh --channel
```

To change it, give the channel name:

```bash
./streamerbot.sh --channel stable
./streamerbot.sh --channel latest
```

Or use the menu:

1. Run `./streamerbot.sh` with no arguments.
2. Choose 6, Auto-Updates and Update Channel.
3. Choose 4, Choose Update Channel.
4. Choose 1 for stable or 2 for latest.

The auto-updater applies the change within five minutes. Check for Updates
(option 5 in the main menu) applies it at once.

Moving from latest to stable never takes a server back to an older version
on its own, because an older version may not understand settings a newer one
has already changed. The server keeps what it has until the next release passes
it. The menu offers to move back at once, and warns before doing so.

The choice is saved in `update_channel.env` beside `streamerbot.sh`, which
updates never overwrite. A new installation starts on stable.

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
have made a sign-in code impossible for a screen reader user to copy — for
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
