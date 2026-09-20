# Changelog

Every change gets a number in square brackets. Numbers only ever go up and are
never reused, so `[004]` refers to one change permanently and can be cited in an
issue or a commit message. Numbering continues across releases.

## Unreleased

- **[041]** The rule that every coding task publishes an artifact page now applies only where a
  browser exists: Windows hosts and Linux desktops publish; a headless Linux command-line session
  does not, and reports in the terminal instead. The page is opened in a browser, and a headless
  server has none, so the page was work nobody could open. Changes CLAUDE.md and the plan.

- **[040]** The manager can now allow each bot's account portal port through ufw, from
  Manage Bots, option 15, or `streamerbot.sh --firewall`.

  A free port was only half of reachability: with ufw on, the portal bound
  correctly and the link still would not open, and the only advice was "allow it
  through your firewall" with no help doing so. It checks whether ufw is
  installed and active, says per bot whether the portal port is allowed, prints
  the exact `ufw` command it runs and what ufw answered, and asks before
  changing anything. When a bot's port has changed it offers to remove the old
  rule; only rules it created (commented `StreamerBot portal <bot>`) are ever
  removed. After ports are handed out on create, restore, Start All and
  Restart All it adds missing rules by itself, but only if ufw is already active.

  Only the portal port is opened. go-librespot's API port and the stream relay
  are loopback-only on purpose. A portal listening on 127.0.0.1 or switched off
  is reported and skipped, since opening its port would do nothing. ufw is never
  enabled from here: on a remote host that can cut off the SSH session running it.
  Tested against a stub ufw; the real ufw's output format is assumed from 0.36.

- **[039]** Creating a bot, and editing bots, now ask whether the account portal
  should be reachable from other computers.

  `auth_portal.host` has always decided this: `127.0.0.1` is the machine the bot
  runs on and nothing else, `0.0.0.0` is every interface. The setting worked. The
  only way to reach it was to edit JSON by hand, over SSH, on a headless server —
  while the bot itself printed "set auth_portal.host to 0.0.0.0 in this bot's
  config.json" whenever it detected the problem
  (`bot/modules/public_address.py::reachability_warning`). A workaround the
  software tells you to perform is a question it should have asked.

  It is asked rather than defaulted to `0.0.0.0`, and the question says what the
  choice costs. The portal takes account passwords and one-time codes and has no
  login page by design: a link is minted only by a TeamTalk command from a user
  who already passed `check_access`, and the token in it is the proof. That is
  sound against someone guessing a URL and it is not encryption, so opening the
  portal to the network puts those links, and what is typed into them, on it in
  plain HTTP. Choosing to open it also prints the firewall sentence, because a
  free port is only half of reachability. Pressing Enter still gives loopback.

  One function, `ask_portal_host`, serves both callers. Two copies of this
  question would drift, and the sentence about what it costs is the half most
  likely to be dropped from the copy. In Bulk Update Configuration it is option
  7, and "Everything" moved from 7 to 8.

- **[038]** A restore, or a bot folder copied in by hand, now keeps the YouTube
  sign-in it arrives with instead of deleting it, and no longer gives advice
  about a device code that no longer exists.

  This reversed twice and the messages went stale with it. The old TTMediaBot
  played YouTube from a `cookies.txt` in the top of the bot's folder. Phase 2
  replaced that with an OAuth device code, so the migration deleted the file and
  said "This version signs in with a code instead. Send li yt." Phase 9 then
  retired the device code as well, because YouTube answers 400 to every
  OAuth-authenticated player request and refuses anonymous playback from
  datacenter addresses. Cookies are how it signs in again. So the migration was
  destroying a working account and, on the way out, directing people to a screen
  that had been removed two phases earlier.

  The file is now staged as `youtube_auth/imported_cookies.txt` and imported by
  the bot a couple of minutes after it starts. It is deliberately **not** written
  straight to `youtube_auth/cookies.txt`, where the bridge reads: that file and
  the bot's own Chrome profile have to agree, because the keep-alive asks Chrome
  whether YouTube still considers it signed in, and a session present only in the
  file answers no — a perfectly good session would be marked expired on its first
  refresh. Importing goes through `YouTubeSessionKeeper.import_text`, the same
  path as a session pasted into the portal, which loads it into the profile
  first, and which already handles arm64 having no Chrome at all.

  Three rules the staging keeps. A file carrying no Google sign-in is not kept,
  checked the same way `bot/auth/cookies.py::has_google_session` checks it — one
  of SAPISID or a `__Secure-` twin *and* one of SID or a twin, since either alone
  is not signed in. A bot that is already signed in keeps the account it is
  using, because the staged file came out of a backup and can only be the older.
  And a session YouTube refuses is renamed rather than deleted: a restore does
  not destroy what it was given, and a file named for the reason it was refused
  is something a user can act on.

  Signing out removes a staged file too. Without that, a bot signs itself back in
  minutes after someone deliberately signed it out, with nothing on screen to
  explain it.

  The port-conflict note was wrong in the same way and is corrected. It told
  people "YouTube and Spotify sign-in are unaffected: those use a code in the
  channel rather than the portal." YouTube's sign-in *is* a portal page now —
  `li yt` mints a `/connect/yt` or `/import/yt` link — so a portal that cannot
  bind does stop it, and the note sent people looking in the wrong place. Spotify
  genuinely is unaffected, and that distinction is now what the note makes.

- **[037]** Spotify and Amazon Music search for albums, artists and playlists as
  well as songs, and a bare `p QUERY` still plays the song.

  Spotify asked `/search` for `type=track` only, so however well the list
  labelled its results there were never any albums or artists in it to label. It
  now asks for all four types in one request. Selecting a container with `sl`
  expands it into its tracks through the same `get()` a pasted link already uses,
  rather than handing the daemon an album URI where it expects a track — an
  artist URI in particular names nothing that can be played at all. The list
  reads "Album: Lifer, by MercyMe" while `Playing` still says
  "MercyMe - Even If", because the queue, the recents list and every existing
  message use that second form and changing it was not part of this.

  Amazon Music already returned the four kinds, and ordering containers first
  therefore meant a bare `p QUERY` started whichever album or artist the page
  rendered first. Both services now promote their best single song to a top
  result, which is what Apple Music gets from its own search page, so the
  containers can lead the list without the bare command losing the song.

  Two more defects in the Amazon search, found while there. Its URL interpolated
  the query raw into a path segment, so "AC/DC" asked for the album listing of an
  artist called AC and anything after a question mark was dropped; it is encoded
  now. And it waited a flat 3.5 seconds before scraping, which on a slow host
  returned nothing and on a fast one could catch a partial render — the same
  failure Apple Music was measured hitting, where the settled top result was
  absent from the set read too early. It now reads until the list stops changing.
  It also picks up the artist from the surrounding row, so its lines name one.

  The ordering, labelling and top-result rules moved to `bot/services/results.py`.
  They lived in `browser_service.py` while the four browser services were the
  only users; Netflix borrowed them, then Spotify, and a third borrower is where
  shared vocabulary stops being a detail of one module. Apple Music's own copy of
  the spoken-title wording is gone with it.

- **[036]** Every coding task now publishes an auto-updating Artifact page and
  hands back the link, recorded as a convention in `CLAUDE.md` and the plan.
  Terminal scrollback is the worst medium for this project's users: it cannot be
  navigated by heading, a long tool result buries the one sentence that matters,
  and re-reading it means arrowing through output written for a machine. The
  page is republished to the same URL as the work moves, is held to the same
  screen reader standard as the web portal, names what is still unverified
  rather than only what passed, and ends with a summary that stands on its own.
  It is a view of the work; `CHANGELOG.md` and the plan remain the record.

- **[035]** `python tools/compile_locales.py` works again on a checkout whose
  path contains a space. It built one interpolated string per babel command and
  ran it with `shell=True`, so the shell split the path and babel was handed
  half a directory name; it exited non-zero and the script reported "Bable is
  not installed" for a babel that was installed and working. The commands are
  now argument lists run without a shell. Found while regenerating the catalogs
  for the strings below, which is the documented workflow, so on such a host the
  documented workflow could not be run at all.

  The same script also wrote absolute paths into every `#:` source reference in
  the `.pot` and `.po` files. Catalogs generated inside the image said
  `/work/bot/__init__.py`, and running the documented command on a developer's
  machine rewrote all 390 of them to that machine's own path — an unreadable
  diff, and a local path committed to the repository. Babel additionally wraps a
  path containing a space in bidi isolate characters, so on such a host the
  references stopped being plain text. Paths are relative now and every babel
  command runs from the repository root, so the output is the same wherever it
  is generated. Normalising the existing `/work/` prefixes is a one-time part of
  this change and accounts for most of its diff.

- **[034]** `sv SERVICE h` now explains the service. Every service set
  `self.help = ""` in its constructor and nothing ever set it to anything else,
  so `sv am h`, `sv yt h` and `sv az h` all answered "This service has no
  additional help" — while the `sv` listing was telling users to send exactly
  that command. Each service now says what it plays, what it needs, what it
  cannot do, and the command that connects it, with its current status on the
  first line so the answer is about this bot rather than the service in general.
  Spotify's says plainly that pairing the account and being able to search by
  name are two separate things, which is the case most likely to leave someone
  concluding their pairing failed when it did not.

- **[033]** `sv` now works out whether a service is ready at the moment it is
  asked, instead of repeating a string left behind by startup. `initialize()`
  set "Apple Music is not ready yet." when no browser engine was attached yet —
  true at that instant, because the services are built before the engine exists
  — but `attach_engine()` never cleared it and nothing else ever wrote to that
  field. The warning therefore outlived its condition for the whole life of the
  process, and `sv am` reported Apple Music as not ready in the same minute
  Apple Music was streaming into the channel. `sv SERVICE` now reports disabled
  with the reason, not connected with the command that fixes it, or connected
  and ready; the sign-in state comes from the same portal lookup `li` uses, so
  the command still answers instantly. Deliberately not a live browser check:
  that is several seconds, and until [031] it would also have stopped the music
  to answer. Netflix and Spotify had the same uncleared warning and are fixed
  with it.

- **[032]** Search results mode lists every result, each named by what it is.
  Two separate limits were both 1: the `slc` count defaulted to 1, so `p QUERY`
  asked the service for a single result and read it back as a numbered list with
  one entry in it, which is not a choice. `slc` now defaults to 25 and `slc 0`
  means as many as the service returns; `services.*.search_results` keeps its
  value of 1, because that one is the bare `p` that plays the best match and
  genuinely wants one. The list also says what each entry is — "1. Album: Abbey
  Road", "2. Track: A Song" — from a formatter that was written in Phase 6,
  tested, and never actually called: the command printed bare titles, so an
  album, an artist and the song on that album were three identical-looking
  lines. Netflix is its own class rather than a browser service and its results
  carry the same kind, so it borrows the same labelling instead of being the one
  service whose list still read back as bare titles. Services whose results
  genuinely have no kinds — YouTube, Spotify — still get a plain list rather
  than every line labelled identically.

- **[031]** Searching no longer stops the music on Apple Music, Amazon Music,
  Netflix and Disney+. The browser engine kept one Chrome tab per service, and
  that tab is where the audio is: Apple Music's search navigates to the search
  page, and checking whether the service is signed in navigates to the home
  page, so both ended whatever was playing. Nothing reported it, because nothing
  failed — no exception, no end-of-track event, and `Player.state` still said
  Playing, so it could only be noticed as behaviour and described in prose. Each
  service now has a player tab that only playback may navigate and an auxiliary
  tab for search, sign-in checks, the login form, profiles and session
  export/import. Both live in the same browser profile, so they share the
  sign-in. In search results mode this is what lets `p QUERY` read out a list
  while the current track keeps playing until `sl NUMBER` picks from it.

- **[030]** Restoring or adopting a configuration from TTMediaBot now recognises
  it by shape rather than by its version number, and repairs its default service
  on disk. Two gaps, both of which let a foreign configuration through looking
  fine. First, `bot_dir_is_legacy` decided lineage from the TTMediaBot cache and
  log filenames, so a config from a fork that had renamed those but still carried
  `services.vk` or `services.yam` was reported as already current and said so to
  the user. `bot/migrators/config_migrator.py` has always decided this by shape,
  for the reason recorded there — a fork that reached its own version 2 means
  something entirely different by it — and the shell now decides it the same way.
  Second, the shell migration never touched `services.default_service`, so a
  restored bot kept `"vk"`, which this bot does not have; `ServiceManager` looks
  that up in a plain dict, so the bot died during startup with a traceback rather
  than anything a user could act on. The bot's own migration repaired this at
  startup, so it was survivable, but only after a restart and only in memory
  until the config was rewritten. It is now set to `yt` when the migration runs,
  and the change is named out loud because it changes which service a bare search
  uses.

- **[029]** A bot folder copied into `bots/` by hand can now be adopted from the
  menu: Manage Bots, then "Adopt Bot Folders Copied Into bots/". Backup and
  Restore was the only supported route, but copying the folder straight in over
  scp or a file manager is the obvious thing to do when the folder is right
  there, and it failed silently. Every menu item works from
  `docker ps -a -f label=role=streamerbot`, so a folder with no container was
  absent from all of them — no error, nothing in any log, because no code ever
  ran for it. The manager now also says so on startup rather than leaving it to
  be discovered.

  Adopting scans for folders with no container, reports what each one is
  (nickname, server, and whether it came from an older version) before anything
  is changed, and only then asks. It puts the folder through the same migration
  a restore uses, so there is one set of rules rather than two that drift: the
  new configuration sections and services are added, TTMediaBot's cache and log
  files are renamed keeping their contents, the original is kept as
  `config.json.pre-migration`, and the nickname, account, server and channel are
  never touched. The account portal, go-librespot and stream relay ports are made
  unique, because a copied folder arrives holding the ports it had on the machine
  it came from and would take them from whichever bot already has them.

  Three cases it refuses rather than guesses at: a folder name that cannot be a
  bot name (a space is the usual reason — the name becomes the `bot_id`, which is
  what stops one bot reaching another's YouTube session, so the name has to meet
  that rule rather than the rule being relaxed); a `config.json` that is not
  valid JSON; and a folder holding several `config.json` files, where there is no
  way to tell which is the bot's. Where someone copied a whole installation
  rather than one bot's data folder, the bot's own files are moved up and the
  source tree beside them is left alone.

  A folder that already has a container is never a candidate, including a stopped
  one, so a bot someone deliberately stopped is not rebuilt behind their back.

- **[028]** Translation catalogs regenerated. They had not been updated since
  Phase 0, so every string added since then, including all of Phase 9's portal
  pages and chat messages, never reached translators and appeared in English in
  every locale. The new strings are now in the `.pot` and all seven `.po` files,
  still untranslated. The compiled `.mo` files are smaller because they had been
  built from older `.po` files and held entries the code no longer uses. No
  translation the code still uses was lost. The one entry that dropped out (the
  search results count help, in Arabic and Hungarian) was already marked fuzzy,
  and its translation said the count resets to 5 when it resets to 1.
- **[027]** YouTube signs in as a real browser session (Phase 9). A bot on a VPS
  could not play YouTube at all. Its log from 10 to 13 September shows 3,577 failed
  attempts to get a stream and not one success. Signed in with the device code,
  YouTube answered 400 to every request. Signed out, every client answered
  `LOGIN_REQUIRED: Sign in to confirm you're not a bot`, because YouTube does not
  trust datacenter addresses. The one thing YouTube still plays for is a real
  browser session: its cookies, plus a proof-of-origin token tied to the account.

  - **Signing in.** `li yt` now sends a portal link instead of a device code. The
    page signs a Google account in through the bot's own Chrome, in its own
    profile. When Google asks for approval on a phone, the page says so, shows the
    number to tap if there is one, and has an "I have approved it on my phone"
    button. When Google asks for a typed code, the existing code page is used.
    Every page recommends a Google account made for the bot, because Google may
    restrict an account that plays automatically from a server. The Google
    password is not stored.
  - **Importing.** When Google refuses the bot's browser (it often refuses
    automated ones), or on ARM where there is no Chrome, `/import/yt` accepts a
    cookies.txt file exported from your own browser. You can choose the file or
    paste its contents. Where Chrome is available, the imported cookies go into
    the bot's profile, and YouTube is asked whether they are signed in before they
    are accepted. The pasted text is never shown back on an error.
  - **Keeping it alive.** Every six hours (`services.yt.session_refresh_hours`)
    the bot loads youtube.com in that profile, which is what makes Google rotate
    the session. It asks YouTube itself whether the session is still signed in
    (`ytcfg.LOGGED_IN`) and stores the new cookies. Cookie expiry dates are not
    used, because Google ends sessions long before them. The schedule counts from
    the last check saved on disk, so a bot that restarts often still gets
    refreshed. A session Google has ended is marked and not retried until someone
    connects it again.
  - **The bridge** reads `youtube_auth/cookies.txt` and `session.json`. Signed
    in, it ties the proof-of-origin token to the account's DataSync ID instead of
    `visitorData`; a token tied to the wrong one is silently ignored. Its session
    cache key includes the cookie file's modification time, so a refresh rebuilds
    that one bot's session and the shared container is never restarted. The file
    is only rewritten when the cookies actually changed. The device-code routes
    `/auth/start` and `/auth/signout` are gone.
  - **What the requester hears.** A request that YouTube refuses for want of a
    sign-in no longer gets "The selected service is currently unavailable". If the
    session looks alive, the reply is "Renewing the YouTube sign-in. <request> will
    start playing when it finishes". The renewal runs in the background, at most
    once every ten minutes, and exactly one more message follows: the request
    playing, or why it did not. A request that arrives during a refresh is held
    and played afterwards, not dropped. A session Google has ended gets "Google
    signed StreamerBot out of YouTube. To sign in again, send this command: li yt".
    With no session at all, the reply says how to connect one.
  - `yl` now only reports the sign-in state and signs the bot out.
  - The Spotify pairing page's "Check status" button posted to the YouTube page;
    it now posts to Spotify's.

  The portal pages were reviewed for accessibility before they were written. That
  review changed how the number to tap is shown: it is a plain readonly number,
  not spelled out digit by digit like a device code, because the phone's own
  screen reader says "eighty-eight" and the two must match. It also added the
  file picker to the import page.

  **Not yet tried against Google or YouTube.** Google's sign-in selectors,
  exporting the session from Chrome, and the DataSync ID binding all follow
  Google's and youtubei.js's documented behaviour. The first run on the VPS that
  answers `LOGIN_REQUIRED` is the test that confirms them. If sign-in fails, the
  bot's log names the Google page it stopped on. The bot-restart fallback described
  in the plan was not built, because nothing so far needs it.

- **[026]** Download what Apple Music is playing. `dl` downloads the song playing
  and uploads it to the channel; `dlp` with no link downloads the album or
  playlist being played as one zip. If a single song is playing, `dlp` gets the
  album that song is on. Before this, `dl` answered "Live streams cannot be
  downloaded", because an Apple Music track is the browser playing, not a file,
  and the gamdl wrapper from Phase 6 was never called by any command.

  - **The song comes from MusicKit, not the bot's queue.** `p <album>` puts one
    track in the bot's queue but eleven in MusicKit's, so only MusicKit's
    `nowPlayingItem` knows which song is playing. A song from the account's
    library has an id gamdl cannot use, so its catalog id is used instead.
  - **gamdl uses the bot's own sign-in.** It needs a cookies.txt with Apple's
    `media-user-token`. That file is now written from the signed-in Apple Music
    browser profile, so the account connected with `li am` is the one that
    downloads, and nobody exports a file by hand. It goes in a job folder under
    the bot's `data/`, readable only by the bot, and is deleted when the job ends,
    even if the download fails. With no token, the user is told to send `li am`.
  - **One message at the start, one at the end**, following the chat rules: the
    first names what is downloading (and mentions `dlp` while an album or
    playlist plays), and the last says it is in the channel or why it failed.
  - **One Apple Music download at a time per bot.** Decrypting a long album is
    heavy on a small VPS, so a second request is refused and the user is told to
    try again when the first finishes.

  `Uploader.upload_file` is now separate from downloading, so a file already on
  disk can be uploaded with the same wait, error reporting and
  `delete_uploaded_files_after` handling. Not yet tested against Apple's live
  site: the MusicKit fields and the cookie export follow Apple's and
  Playwright's documented shapes, and the first real `dl` on a signed-in bot is
  the test that confirms them.

- **[025]** Apple Music plays what you search for. With sign-in working,
  every request failed with "Apple Music did not start playing:
  Page.wait_for_function: Timeout 30000ms exceeded", and a search for "adventure
  of a Lifetime" tried to play the account's "Favorite Songs" playlist. Three
  faults, each measured against Apple's live site in the bot's own Chrome:

  - **Nothing was ever queued.** `play()` opened the item's page and called
    MusicKit's `play()` with an empty queue, which does nothing, so the 30-second
    wait always ran out. `setQueue({url})` then `play()` started a song and an
    album at once. An empty queue now fails immediately with a reason; an artist
    measured 39 seconds before and under 8 now.
  - **The search address had no storefront.** `music.apple.com/search?term=...`
    redirects to `/us/search` and turns every space into a literal plus sign, so
    Apple searched for "adventure+of+a+lifetime" and ranked an artist named
    Chubb+Bits first. The storefront is now read from MusicKit, which is the
    signed-in account's own country, and put in the address directly: the
    Coldplay song came first, "AC/DC" kept its slash and "C++" its plus signs.
  - **The scraper took every link on the page**, including the signed-in
    sidebar's library, which is where "Favorite Songs" came from. Only result
    links inside `main` are taken now, never navigation, `/library/` or music
    videos.

  Apple's own Top Results now lead, in Apple's order, each saying what it is
  ("Top result: Song: Adventure of a Lifetime, by Coldplay"), since that group's
  label alone does not. An artist never leads, because `p <query>` plays the first
  result and an artist cannot be queued. The scrape waits for the results to stop
  changing rather than a fixed delay. A playback timeout names the likely cause —
  subscription, or a signed-out session and `li am` — instead of passing a
  Playwright error to the channel.

  Verified live signed out, where Apple plays 30-second previews: `p adventure of
  a LIfetime` played Coldplay's song in 1.4 seconds and an album played. Full
  tracks need the signed-in bot, which is the test that confirms it.

- **[024]** The auto-updater no longer rebuilds and restarts every bot for a push
  that changes no bot code. When GitHub moves, it fetches and checks which files
  changed; if all of them are under `.claude/` (the plan and hooks), `.github/`
  (CI and issue forms, which run on GitHub) or are Markdown documents, it skips
  the rebuild, the restart and the announcement in every channel, and logs that it
  did so once rather than every five minutes. A push that changes code and a
  CHANGELOG entry together still updates, a skipped push followed by a code push
  updates with both, and any doubt — an unreadable or empty diff — means update.
  Tests are deliberately not on the skip list, since a needless rebuild is a far
  cheaper mistake than a skipped one. Manual updates are unchanged.

- **[023]** Apple Music sign-in finds Apple's real form. It failed every time with
  "The Apple sign-in form did not appear", because the adapter was written against
  a guess of the page and never checked. Measured in the bot's own Chrome:
  `music.apple.com/login` opens a dialog that spins for 10 to 20 seconds, then shows
  "Continue with Email" — one box and a Continue button — in a
  `commerce/authenticate` frame. Apple's own password form sits inside that on
  `idmsa.apple.com`, zero pixels tall, with the password step marked `aria-hidden`
  and its button disabled, while Playwright still calls those fields visible.

  The old code took the first frame whose address contained "auth" — on the live
  page that was a frame with no form in it at all — and gave up after 10 seconds.
  The new code waits up to a minute, looks only at fields a person could actually
  see (not inside `aria-hidden`, not `tabindex="-1"`, and in frames with real size
  on screen), types the email and presses Continue, waits for the password step to
  be exposed rather than merely present, and presses that step's own button. Checked
  against Apple's live page, typing nothing: it finds the email box, correctly does
  not report the hidden password box yet, and chooses Continue.

  Apple's error text is passed on instead of a generic failure, an address Apple does
  not recognise is explained, split six-box verification codes are typed rather than
  pasted into the first box, and "trust this browser" is accepted so the saved sign-in
  lasts. The steps after Continue could not be observed without a real Apple Account
  and are pinned by tests; the first real sign-in is what confirms them. The overall
  sign-in limit rose from 300 to 600 seconds, because the verification code alone may
  wait 300 and the old limit gave up on someone still typing it.

- **[022]** Planned, not built: Phase 9, YouTube sign-in through a real browser
  session. The test bot's log from 10 to 13 September has 3,577 failed stream
  resolution attempts and no successful fetch: the OAuth device code gets a 400
  on every playback request, and anonymous playback is refused as bot traffic
  from the VPS address. The plan replaces device-code sign-in with a Chrome
  session per bot, kept alive by a scheduled per-bot refresh, with a cookie import
  page for when Google refuses automated sign-in. It records why Chrome rather
  than a text browser, why the session is kept alive rather than "renewed", why
  cookies stay out of `config.json`, and why holding a request beats asking the
  user to resend it. It also records that this reverses the Phase 2 decision
  against cookie files.

- **[021]** YouTube and YouTube Music playback no longer hands mpv a googlevideo.com URL directly.
  A new loopback relay (`bot/services/stream_proxy.py`, port `player.stream_proxy_port`, default
  4420) fetches the stream in bounded byte-range windows and hands mpv a local URL instead, because
  Google's CDN answers a bare 403 to the single open-ended request mpv normally makes for anything
  past a per-video byte limit that isn't a fixed constant — a limit low enough that this was hitting
  ordinary, non-obscure tracks, not just edge cases. Without this, `on_end_file`'s stream-refresh retry
  saw the 403 as a normal playback error, re-resolved to an equally-doomed URL, and silently advanced
  to the next track — which looked like the bot racing through 15+ autoplay tracks in under two
  seconds. A window that still 403s is retried at half size rather than failing the track, since the
  CDN's limit was measured to vary by video/session rather than holding at one number.

- **[020]** `YtService.get()` now retries any transient bridge/CDN error up to twice before giving up,
  not only ones whose message looks auth-related. A bare bridge hiccup used to surface as "The selected
  service is currently unavailable" on the very first try.

- **[019]** The YouTube stream-refresh retry (`on_end_file` in `bot/player/__init__.py`) now backs off
  briefly and allows up to 3 attempts instead of exactly 1 with no delay, since a freshly re-resolved
  stream URL can hit the same transient failure the original did.

- **[018]** Fixed "Straemer Bot" in the update-in-progress broadcast from 6d2e477.
  It is sent to every user in the channel and read aloud, so a misspelling of the
  bot's own name is heard by everyone every time an update runs. The wording is
  otherwise untouched.

- **[017]** The auto-updater checks GitHub every five minutes instead of every
  hour. An hour is too long to sit on a fix for something that stops the bot
  working, which is the case that matters. Five minutes is also the floor
  `auto_updater.sh` already enforced, so this is as aggressive as it goes. It
  still does not react to individual pushes: it wakes on the interval, asks
  whether the branch moved, and only rebuilds if it did.

- **[016]** Netflix, Disney Plus, Apple Music and Amazon Music could not sign in
  or play on any bot container that had ever been restarted, which — since bots
  are created with `--restart always` — was all of them after their first start.
  Apple Music sign-in failed with `Target page, context or browser has been
  closed`, and underneath it Chrome had said `Missing X server or $DISPLAY`.

  `/tmp` survives a `docker restart`, so Xvfb's lock file still held the previous
  run's pid — and after a restart that low pid is alive again in the fresh pid
  namespace, so Xvfb concluded a server was genuinely running and exited. This is
  not the stale-lock case Xvfb cleans up itself, which is why a lock naming a dead
  pid does not reproduce it. Measured over four starts of one container: display
  present on the first, absent on all three restarts.

  The lock and socket are now cleared when — and only when — no X server actually
  answers, so a display genuinely in use is never pulled out from under it. Same
  test after the fix: present on all four.

  The entrypoint also printed `OK. Virtual display :99 started` on all four runs,
  including the three failures, because that line sat after the wait loop and
  never consulted it. A start-up line that is always OK is worse than none: it
  points the reader away from the fault. It now reports honestly, names the four
  services that will not work, and shows what Xvfb said.

  Second fault found on the way: Playwright *replaces* the environment when given
  `env=`, so `env={"DISPLAY": ...}` launched Chrome with no `HOME`, no
  `XDG_RUNTIME_DIR` and no `PULSE_*`. That would have been the next failure after
  the display one — a Chrome that cannot find the PulseAudio socket plays into
  nothing, which looks exactly like a site that failed to start the video. The
  environment is now merged rather than replaced.

  No change was needed to how Apple Music signs in. That flow already waits for
  Apple's verification code and asks for it through the portal
  (`apple_music.py`); it was simply never reached, because Chrome died first.

- **[015]** YouTube playback is attested properly, which is what
  `LOGIN_REQUIRED: Sign in to confirm you're not a bot` was really about. After
  [012] all five clients were tried and all five were refused — signed in with a
  400, signed out as bot traffic — so signing out was no escape and the bot
  announced a track and then played silence.

  The proof-of-origin token was bound to the wrong thing. YouTube checks that a
  token's binding matches the identity of the request it arrives on; a token bound
  to anything else is not rejected, it is ignored, and the request is then treated
  as un-attested. From a VPS that means bot detection on every client. For
  youtubei.js the token is **session**-bound — `po_token` is a session option that
  flows into `Session.getSessionData` beside `visitor_data`, and into
  `Player.create` — but it was being minted per call against the **video ID**, and
  the session itself was created with neither a token nor a matching
  `visitor_data`.

  Sessions now learn their own `visitorData` first, mint a token bound to it, and
  are created carrying both. Tokens are cached until they expire. Verified against
  the two videos from the reported log: both resolve, `po-token available=true`,
  and the stream URL returns real audio bytes. The datacenter block itself cannot
  be reproduced from a residential address, so that half is confirmed only on a
  host YouTube distrusts.

  `/health` now reports whether the token provider is reachable, because the
  symptom of it being down is a message about bot detection that reads like an
  account problem and sends you looking in the wrong place entirely.

- **[014]** Issue forms, sorted automatically. A bug report form labels itself
  `bug` and `needs investigation`; a collaboration request form labels itself
  `colab request` and asks for a GitHub username, an email address, why the
  person wants to work on this, and what they would like to work on. Blank issues
  stay enabled and a GitHub Actions workflow labels anything filed without a form
  as `other`, so nothing arrives unsorted and nobody has to remember to sort it.

  Dependabot cannot do this labelling — it only opens dependency pull requests
  and security alerts and never touches issues people file — so the mechanism is
  the forms' own `labels:` field plus that workflow. The workflow uses
  `github-script` rather than a `run:` block and never handles the issue text at
  all, because an issue title is attacker-controlled in exactly the way a commit
  message was when one executed itself in this repository's CI.

  The bug form asks separately whether a log is available, with "yes but I cannot
  share it right now" and "I do not know where to find it" as real answers, and
  says where each bot's log lives. Neither that question nor the paste box is
  required: requiring a log turns "I do not have one" into a reason not to file at
  all. Only "what happened" and "what you expected" are required.

  Both forms say plainly that the issue is public. The bug form says not to paste
  a portal link, which is a bearer credential; the collaboration form says so on
  the email field and offers wording for anyone who would rather send it another
  way, since collecting an address in a public issue without mentioning that is
  signing someone up for spam they did not agree to.

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
