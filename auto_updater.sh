#!/bin/bash
# StreamerBot Auto-Updater Watcher
#
# Two independent cadences share one loop:
#
#   GitHub update check  - every STREAMERBOT_UPDATE_INTERVAL seconds (default one
#                          hour). This does NOT react to individual pushes; it
#                          wakes on the interval and only then asks GitHub
#                          whether the branch has moved.
#   Health check         - every tick (60s), because recovering a dead shared
#                          YouTube service is a self-healing feature and should
#                          not have to wait for the hourly update window.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Shared project constants (repo owner, branch, image name, update interval).
if [ -f "$SCRIPT_DIR/project.env" ]; then
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/project.env"
fi

TICK_SECONDS=60
UPDATE_INTERVAL="${STREAMERBOT_UPDATE_INTERVAL:-3600}"
case "$UPDATE_INTERVAL" in
    ''|*[!0-9]*) UPDATE_INTERVAL=3600 ;;
esac
# Floor: anything more aggressive than five minutes is pointless polling.
[ "$UPDATE_INTERVAL" -lt 300 ] && UPDATE_INTERVAL=300

DEFAULT_BRANCH="${STREAMERBOT_BRANCH:-main}"
IMAGE_NAME="${STREAMERBOT_IMAGE:-streamerbot}"

# Define SUDO dynamically: use sudo if not root
if [ "$EUID" -ne 0 ]; then
    SUDO="sudo"
else
    SUDO=""
fi

# Fix git safe directory issue when running as root on a repository owned by another user (common in VPS)
git config --global --add safe.directory "$SCRIPT_DIR" 2>/dev/null
git config core.fileMode false 2>/dev/null

# Discover real user and set SSH key command dynamically for root (so we can authenticate with user's key)
REAL_USER=$(stat -c '%U' "$SCRIPT_DIR" 2>/dev/null || echo "admin")
REAL_USER_HOME=$(getent passwd "$REAL_USER" 2>/dev/null | cut -d: -f6 || echo "/home/$REAL_USER")
if [ -f "$REAL_USER_HOME/.ssh/id_ed25519" ]; then
    export GIT_SSH_COMMAND="ssh -i $REAL_USER_HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new"
fi

echo "StreamerBot Auto-Updater started. Checking GitHub every ${UPDATE_INTERVAL} seconds."

# Force a GitHub check on the first pass, then every UPDATE_INTERVAL seconds.
LAST_GITHUB_CHECK=0

YOUTUBE_BRIDGE_URL="http://127.0.0.1:4417"
RECOVERY_BACKOFFS=(20 40 80 160 300)
RECOVERY_FAILURES=0
NEXT_RECOVERY_AT=0

shared_youtube_service_supported() {
    $SUDO docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 \
        && $SUDO docker run --rm --entrypoint test "$IMAGE_NAME" \
            -f /home/streamer/StreamerBot/youtube_services.sh
}

# Cleanup function
cleanup() {
    echo "$(date): Auto-Updater shutting down..."
    # Kill background sleep if running so we exit immediately
    [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null
    exit 0
}

# Trap signals for clean shutdown (fixes systemd SIGTERM timeout)
trap cleanup INT TERM

# Function to check if local is behind remote
# Note: we only compare hashes. git ls-remote doesn't download objects,
# so git merge-base would fail with 'Not a valid commit name'.
is_behind_remote() {
    local local_h=$1
    local remote_h=$2
    if [ "$local_h" == "$remote_h" ]; then return 1; fi
    # Hashes differ = remote has changed = we need to update
    return 0
}

reset_recovery_backoff() {
    RECOVERY_FAILURES=0
    NEXT_RECOVERY_AT=0
}

schedule_recovery_retry() {
    local index=$RECOVERY_FAILURES
    local last_index=$((${#RECOVERY_BACKOFFS[@]} - 1))
    if [ "$index" -gt "$last_index" ]; then
        index=$last_index
    fi

    local delay=${RECOVERY_BACKOFFS[$index]}
    RECOVERY_FAILURES=$((RECOVERY_FAILURES + 1))
    NEXT_RECOVERY_AT=$(($(date +%s) + delay))
    echo "$(date): Shared YouTube recovery failed. Retrying in ${delay}s."
}

while true; do
    # 0. Check for version lock (pin)
    if [ -f "$SCRIPT_DIR/.no_update" ]; then
        # Disallow auto-update if file exists
        sleep 60
        continue
    fi
    SHOULD_UPDATE=false
    RECOVERY_ATTEMPT=false

    # 1. Update detection. Rate limited to once per UPDATE_INTERVAL so that a
    #    burst of pushes does not cause a burst of rebuilds.
    NOW=$(date +%s)
    if [ $((NOW - LAST_GITHUB_CHECK)) -ge "$UPDATE_INTERVAL" ]; then
        LAST_GITHUB_CHECK=$NOW

        BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$DEFAULT_BRANCH")

        # ls-remote does not download objects and is not rate limited.
        REMOTE_HASH=$(git ls-remote origin -h "refs/heads/$BRANCH" 2>/dev/null | awk '{print $1}' | tr -d '[:space:]')
        LOCAL_HASH=$(git rev-parse HEAD 2>/dev/null | tr -d '[:space:]')

        # What the running image was actually built from.
        RUNNING_HASH=$($SUDO docker inspect "$IMAGE_NAME" --format '{{ index .Config.Labels "commit_hash" }}' 2>/dev/null | tr -d '[:space:]')
        if [ -z "$RUNNING_HASH" ] || [ "$RUNNING_HASH" = "<novalue>" ] || [ "$RUNNING_HASH" = "<noopt>" ] || [[ "$RUNNING_HASH" == *"<no"* ]]; then
            RUNNING_HASH="none"
        fi

        if [ -n "$REMOTE_HASH" ]; then
            if is_behind_remote "$LOCAL_HASH" "$REMOTE_HASH"; then
                echo "$(date): New version detected on GitHub ($REMOTE_HASH). Triggering update..."
                SHOULD_UPDATE=true
            elif [ "$LOCAL_HASH" != "$RUNNING_HASH" ]; then
                echo "$(date): Local code ($LOCAL_HASH) does not match running image ($RUNNING_HASH). Syncing..."
                SHOULD_UPDATE=true
            fi
        fi
    fi

    if shared_youtube_service_supported; then
        if curl -fsS "$YOUTUBE_BRIDGE_URL/health" >/dev/null 2>&1; then
            reset_recovery_backoff
        elif [ "$(date +%s)" -ge "$NEXT_RECOVERY_AT" ]; then
            echo "$(date): Shared YouTube service unavailable. Triggering recovery..."
            SHOULD_UPDATE=true
            RECOVERY_ATTEMPT=true
        fi
    fi

    if [ "$SHOULD_UPDATE" = true ]; then
        echo "$(date): Running update.sh..."
        HASH_BEFORE=$(git rev-parse HEAD 2>/dev/null)
        $SUDO env AUTO_UPDATE=true "$SCRIPT_DIR"/update.sh
        UPDATE_EXIT=$?
        HASH_AFTER=$(git rev-parse HEAD 2>/dev/null)

        if [ "$UPDATE_EXIT" -eq 75 ]; then
            echo "$(date): Another update owns the lock. Skipping cycle."
        else
            echo "$(date): update.sh finished with exit code $UPDATE_EXIT"
        fi

        # CRITICAL: update.sh does 'git reset --hard' which replaces THIS script
        # on disk. We must re-exec to pick up the new version, otherwise systemd
        # detects 'command vanished from unit file' and kills us.
        # Only exec if code actually changed to avoid infinite loop.
        if [ "$HASH_BEFORE" != "$HASH_AFTER" ]; then
            echo "$(date): Code updated ($HASH_BEFORE -> $HASH_AFTER). Re-launching..."
            exec "$SCRIPT_DIR/auto_updater.sh"
        fi

        if [ "$RECOVERY_ATTEMPT" = true ] && [ "$UPDATE_EXIT" -ne 75 ]; then
            if curl -fsS "$YOUTUBE_BRIDGE_URL/health" >/dev/null 2>&1; then
                reset_recovery_backoff
            else
                schedule_recovery_retry
            fi
        fi
    fi
    # Interruptible sleep: runs in background so SIGTERM can stop us immediately
    sleep "$TICK_SECONDS" &
    SLEEP_PID=$!
    wait "$SLEEP_PID" 2>/dev/null
    SLEEP_PID=""
done
