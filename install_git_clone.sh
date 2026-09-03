#!/bin/bash
#
# StreamerBot one-shot installer.
#
# Installs git if it is missing, clones the StreamerBot repository, installs
# Docker and the other host tools the manager needs, then hands over to
# streamerbot.sh.
#
# Output is written for screen readers: plain text, no emoji, no box drawing,
# no progress bars that rewrite their own line, and every status line starts
# with the word OK, Warning or Error rather than relying on colour.

set -u

# ---------------------------------------------------------------------------
# Repository. Kept in sync with project.env once the clone exists; these are
# the bootstrap values used before there is a project.env to read.
# ---------------------------------------------------------------------------
REPO_OWNER="kcrpine"
REPO_NAME="StreamerBot"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"
DIR_NAME="${REPO_NAME}"

step() { echo; echo "Step $1. $2"; }
ok() { echo "OK. $1"; }
warn() { echo "Warning. $1"; }
fail() { echo "Error. $1"; exit 1; }

if [ "$EUID" -ne 0 ]; then
    echo "This installer needs root to install packages. Re-launching with sudo."
    exec sudo bash "$0" "$@"
fi

echo "StreamerBot installer"
echo "This will install git and Docker if they are missing, download StreamerBot, and start the manager."

# ---------------------------------------------------------------------------
# Package manager abstraction.
# ---------------------------------------------------------------------------
PKG_MANAGER=""
detect_package_manager() {
    for candidate in apt-get dnf yum pacman zypper apk; do
        if command -v "$candidate" > /dev/null 2>&1; then
            PKG_MANAGER="$candidate"
            return 0
        fi
    done
    return 1
}

install_packages() {
    [ "$#" -eq 0 ] && return 0
    case "$PKG_MANAGER" in
        apt-get) DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
        dnf)     dnf install -y "$@" ;;
        yum)     yum install -y "$@" ;;
        pacman)  pacman -Sy --noconfirm "$@" ;;
        zypper)  zypper --non-interactive install "$@" ;;
        apk)     apk add --no-cache "$@" ;;
        *)       fail "No supported package manager found. Please install these by hand and run this again: $*" ;;
    esac
}

ensure_command() {
    # ensure_command <command> <package name>
    local cmd="$1" pkg="$2"
    if command -v "$cmd" > /dev/null 2>&1; then
        ok "$cmd is already installed."
    else
        echo "$cmd is not installed. Installing the $pkg package now."
        install_packages "$pkg" || fail "Could not install $pkg."
        command -v "$cmd" > /dev/null 2>&1 || fail "$pkg installed but $cmd is still not available."
        ok "$cmd installed."
    fi
}

step 1 "Checking the package manager."
detect_package_manager || fail "No supported package manager found. StreamerBot supports apt, dnf, yum, pacman, zypper and apk."
ok "Using $PKG_MANAGER."

step 2 "Checking for git."
ensure_command git git

step 3 "Downloading StreamerBot."
if [ -d ".git" ] && git remote get-url origin 2>/dev/null | grep -qi "${REPO_NAME}"; then
    ok "Already inside a ${REPO_NAME} checkout. Skipping the clone."
    REPO_DIR="$(pwd)"
elif [ -d "$DIR_NAME/.git" ]; then
    ok "Found an existing $DIR_NAME directory. Updating it."
    cd "$DIR_NAME" || fail "Could not enter $DIR_NAME."
    git pull --ff-only || warn "Could not fast-forward. Leaving the existing checkout as it is."
    REPO_DIR="$(pwd)"
else
    echo "Cloning $REPO_URL"
    git clone "$REPO_URL" "$DIR_NAME" || fail "Could not clone the repository. Check the network connection and that the repository exists."
    cd "$DIR_NAME" || fail "Could not enter $DIR_NAME."
    REPO_DIR="$(pwd)"
    ok "Repository downloaded to $REPO_DIR."
fi

step 4 "Checking the host tools StreamerBot needs."
# jq   - streamerbot.sh edits each bot's config.json with it
# curl - update checks and the TeamTalk SDK download
# tar  - backup and restore
for pair in "jq jq" "curl curl" "tar tar"; do
    # shellcheck disable=SC2086
    set -- $pair
    ensure_command "$1" "$2"
done

step 5 "Checking for Docker."
if command -v docker > /dev/null 2>&1; then
    ok "Docker is already installed."
else
    echo "Docker is not installed. Installing it from the official Docker convenience script."
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh || fail "Could not download the Docker installer."
    sh /tmp/get-docker.sh || fail "The Docker installer failed. Install Docker by hand and run this again."
    rm -f /tmp/get-docker.sh
    command -v docker > /dev/null 2>&1 || fail "Docker installed but the docker command is still not available."
    ok "Docker installed."
fi

if docker info > /dev/null 2>&1; then
    ok "The Docker service is running."
else
    echo "The Docker service is not running. Starting it."
    systemctl enable --now docker > /dev/null 2>&1 || service docker start > /dev/null 2>&1 || true
    if docker info > /dev/null 2>&1; then
        ok "The Docker service is running."
    else
        warn "Could not start the Docker service automatically. Start it yourself, then run streamerbot.sh."
    fi
fi

step 6 "Setting ownership and permissions."
REAL_USER="${SUDO_USER:-$USER}"
git config core.fileMode false 2>/dev/null || true
chown -R "$REAL_USER":"$REAL_USER" . 2>/dev/null || true
chmod +x ./*.sh 2>/dev/null || true
ok "Files now belong to $REAL_USER."

# The TeamTalk SDK is no longer downloaded here. It is fetched from bearware.dk
# inside the Docker image build, using the URLs in project.env, so the host does
# not need a TeamTalk_DLL directory at all.

step 7 "Starting the StreamerBot manager."
if [ ! -f "./streamerbot.sh" ]; then
    fail "streamerbot.sh was not found in $REPO_DIR. The download may be incomplete."
fi
chmod +x ./streamerbot.sh
ok "Setup complete. Starting the manager now."
echo
exec ./streamerbot.sh
