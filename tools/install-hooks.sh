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
echo
echo "The pre-commit hook does two things:"
echo "  It refuses to commit private files from .claude, such as the OAuth"
echo "  credentials and your session transcripts."
echo "  It refuses to commit a plan that has drifted from your local copy at"
echo "  ~/.claude/plans, and does nothing if you do not have one."
echo
echo "To bypass it for one commit, use git commit --no-verify."
echo "To undo this, run: git config --unset core.hooksPath"
