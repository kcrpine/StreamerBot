#!/bin/bash
# Update channels: which branch this server updates from.
#
#   stable  The stable branch, which only ever holds released versions. CI moves
#           it to a release's v<version> tag when the release is published, and
#           only ever forward (.github/workflows/stable.yml).
#   latest  The development branch (STREAMERBOT_BRANCH, main): every push.
#
# The updater detects a new version the same way on either channel, with one
# git ls-remote of a branch head. A stable branch costs nothing extra to watch,
# where asking the GitHub releases API every five minutes would be rate limited
# and would need a release tag turned back into something to check out.
#
# The choice belongs to one server, so it lives in update_channel.env beside
# this file, which is gitignored. That matters: update.sh resets the checkout to
# the repository's copy on every update, and reset --hard and clean -fd leave an
# ignored file alone, where a tracked one would be put back as it was.
# STREAMERBOT_DEFAULT_CHANNEL in project.env applies while the file is absent.
#
# Sourced after project.env by update.sh, auto_updater.sh, streamerbot.sh and
# masc.sh. Functions only; SCRIPT_DIR must be set.

UPDATE_CHANNEL_FILE="${SCRIPT_DIR}/update_channel.env"

# Prints stable or latest. Anything unrecognised reads as stable, the channel
# that cannot hand a server unreleased code by mistake.
update_channel() {
    local value=""
    if [ -f "$UPDATE_CHANNEL_FILE" ]; then
        value="$(sed -n 's/^STREAMERBOT_CHANNEL=//p' "$UPDATE_CHANNEL_FILE" | tail -n1 | tr -d '[:space:]')"
    fi
    [ -n "$value" ] || value="${STREAMERBOT_DEFAULT_CHANNEL:-stable}"
    case "$value" in
        latest) echo latest ;;
        *) echo stable ;;
    esac
}

set_update_channel() {
    case "${1:-}" in
        stable|latest) ;;
        *) return 1 ;;
    esac
    {
        echo "# Which branch this server updates from: stable (releases only) or latest (every push)."
        echo "# Set with: streamerbot.sh --channel stable|latest. See update_channel.sh."
        echo "STREAMERBOT_CHANNEL=$1"
    } > "$UPDATE_CHANNEL_FILE"
}

latest_branch() { echo "${STREAMERBOT_BRANCH:-main}"; }
stable_branch() { echo "${STREAMERBOT_STABLE_BRANCH:-stable}"; }

channel_branch() {
    if [ "${1:-}" = latest ]; then latest_branch; else stable_branch; fi
}

# Said aloud by a screen reader, so words rather than branch names.
channel_description() {
    if [ "${1:-}" = latest ]; then
        echo "every change as soon as it is pushed"
    else
        echo "released versions only"
    fi
}

# The branch this server follows. A repository that has never published a
# release has no stable branch, and following a branch that does not exist means
# never updating at all, so that case follows the latest branch instead. Only a
# definite "no such branch" falls back: ls-remote --exit-code exits 2 for that
# and 128 for a network or sign-in failure, which must not move a stable server
# onto development code for one cycle.
update_branch() {
    local branch status
    branch="$(channel_branch "$(update_channel)")"
    if [ "$branch" != "$(latest_branch)" ]; then
        timeout 10 git ls-remote --exit-code origin -h "refs/heads/$branch" >/dev/null 2>&1
        status=$?
        [ "$status" -eq 2 ] && branch="$(latest_branch)"
    fi
    echo "$branch"
}

# True when moving to the given commit would take this checkout backwards: it
# is an ancestor of HEAD and not HEAD itself. That is the usual state of a
# server switched from latest to stable, which runs code newer than the last
# release. Older code refuses a config.json whose config_version is newer than
# it understands (bot/migrators/config_migrator.py), so a switch never moves a
# server back on its own; it stays where it is until a release passes it. The
# commit must already be fetched.
channel_would_downgrade() {
    local target="$1" head
    head="$(git rev-parse HEAD 2>/dev/null)" || return 1
    [ -n "$target" ] && [ "$target" != "$head" ] \
        && git merge-base --is-ancestor "$target" "$head" 2>/dev/null
}

# A release tag where there is one ("v1.0.2"), otherwise a short hash.
describe_commit() {
    git describe --tags --exact-match --match 'v[0-9]*' "$1" 2>/dev/null \
        || git rev-parse --short "$1" 2>/dev/null
}
