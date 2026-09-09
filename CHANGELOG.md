# Changelog

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
