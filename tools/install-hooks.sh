#!/bin/bash
#
# Point this clone's git hooks at the versioned .githooks directory.
#
# Hooks in .git/hooks are not versioned and do not survive a clone, which is why
# they are kept in .githooks and wired up with core.hooksPath instead. This has
# to be run once per clone; git deliberately offers no way to do it
# automatically, since a repository that could install its own hooks could run
# arbitrary code on checkout.
#
# Output follows the project's screen-reader conventions: plain text, no emoji,
# every status line starting with OK, Warning or Error.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -d .githooks ]; then
    echo "Error. No .githooks directory here. Run this from inside the repository."
    exit 1
fi

chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks

echo "OK. Hooks installed for this clone."

# ---------------------------------------------------------------------------
# Seed the local plan on a fresh clone.
#
# Someone who has just cloned this has no plan in ~/.claude/plans, so every
# check that compares the two copies quietly does nothing, and they start work
# without knowing what the earlier phases found. Copy it once; never overwrite
# a local copy that already exists, because that one may be ahead.
# ---------------------------------------------------------------------------
REPO_PLAN=".claude/plans/streamerbot-plan.md"
PLANS_DIR="$HOME/.claude/plans"

if [ -f "$REPO_PLAN" ]; then
    heading="$(head -n 1 "$REPO_PLAN")"
    existing=""
    if [ -d "$PLANS_DIR" ]; then
        for candidate in "$PLANS_DIR"/*.md; do
            [ -f "$candidate" ] || continue
            if [ "$(head -n 1 "$candidate")" = "$heading" ]; then
                existing="$candidate"
                break
            fi
        done
    fi

    if [ -n "$existing" ]; then
        echo "OK. You already have a local plan at $existing. Leaving it alone."
    else
        mkdir -p "$PLANS_DIR"
        cp "$REPO_PLAN" "$PLANS_DIR/streamerbot-plan.md"
        echo "OK. Copied the plan to $PLANS_DIR/streamerbot-plan.md."
        echo "    Read it before starting work. It records what each phase actually"
        echo "    found, including decisions that were later overturned."
    fi
fi
echo
echo "The pre-commit hook does two things:"
echo "  It refuses to commit private files from .claude, such as the OAuth"
echo "  credentials and your session transcripts."
echo "  It refuses to commit a plan that has drifted from your local copy at"
echo "  ~/.claude/plans, and does nothing if you do not have one."
echo
echo "The pre-push hook runs the test suite in both of the places it has to"
echo "run: inside the image, which is the only place bot/ can import its"
echo "dependencies, and on this host, which is the only place the deployment"
echo "tests find the jq they need. Neither half covers the other, and the"
echo "deployment tests skip rather than fail when jq is missing, so an"
echo "in-image run alone reports a pass having skipped all 181 of them."
echo "It takes about two minutes. Missing Docker, image or jq is a warning,"
echo "not a refusal."
echo
echo "To bypass them for one command, use git commit --no-verify or"
echo "git push --no-verify."
echo
echo "To undo this, run: git config --unset core.hooksPath"
