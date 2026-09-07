# Working with StreamerBot in Claude Code

This directory gives anyone working on StreamerBot the same setup the project was
built with, so a contributor using Claude Code starts from the same plan and the
same rules rather than guessing at them.

## What is here

| Path | What it is |
| --- | --- |
| `plans/streamerbot-plan.md` | The full implementation plan: architecture, the accessibility requirements, the phase order, and a running record of what each phase actually found. Read this first. |
| `hooks/` | Three hooks that route user-facing work through the accessibility agents before it is written, rather than reviewing it afterwards. |
| `settings.json` | Wires those hooks up. Paths use `$CLAUDE_PROJECT_DIR`, so it works wherever you cloned the repo. |

## What is deliberately not here

A `.claude` directory on your own machine also holds `.credentials.json`, which
contains a live OAuth access token and refresh token for your Claude account, and
`projects/`, which holds the full transcript of every session you have run. Neither
belongs in a public repository. `history.jsonl`, `cache/`, `sessions/`,
`shell-snapshots/` and `debug/` are machine state and are equally not shared.

The repository's `.gitignore` excludes all of them by name. If you add anything to
`.claude/`, check it against that list first.

## The accessibility agents

The hooks call an agent named `accessibility-lead`, which is **not** vendored here:
it belongs to its own project and is better installed from source than copied. Install
the accessibility agent team into your user-level `~/.claude/agents/`, and the hooks in
this repository will find it.

Without those agents installed the hooks still run and simply print their reminder, so
nothing breaks; you just do not get the automatic review.

## Why the hooks exist

StreamerBot's primary users are blind. The plan commits the web portal to WCAG 2.2 AA
and the CLI to screen-reader-first conventions, and those are treated as requirements
rather than aspirations.

This is not ceremony. During Phase 3 the portal design was reviewed before any markup
was written, and that review overturned five decisions the plan had already marked
settled. The most serious: the OAuth device code was specified as an `aria-hidden`
paragraph, which would have made it unreachable by the screen reader's virtual cursor
and therefore impossible to copy — for exactly the users who most need to copy it
rather than transcribe nine characters by ear. Catching that before it shipped cost one
review. Catching it afterwards would have cost a user their sign-in.

If you are changing the auth portal, the TeamTalk chat messages, or `streamerbot.sh`'s
menus, read the accessibility sections of the plan before you start.
