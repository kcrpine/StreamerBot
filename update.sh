#!/bin/bash

# Auto-detect script location and set paths dynamically
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
BOTS_ROOT="${SCRIPT_DIR}/bots"

# Fix git safe directory issue when running as root on a repository owned by another user (common in VPS)
git config --global --add safe.directory "$SCRIPT_DIR" 2>/dev/null
git config core.fileMode false 2>/dev/null

# Discover real user and set SSH key command dynamically for root (so we can authenticate with user's key)
REAL_USER=$(stat -c '%U' "$SCRIPT_DIR" 2>/dev/null || echo "admin")
REAL_USER_HOME=$(getent passwd "$REAL_USER" 2>/dev/null | cut -d: -f6 || echo "/home/$REAL_USER")
if [ -f "$REAL_USER_HOME/.ssh/id_ed25519" ]; then
    export GIT_SSH_COMMAND="ssh -i $REAL_USER_HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new"
fi

# Shared project constants. Single source of truth for the repository, the image
# name and the pinned external binary versions.
if [ -f "$SCRIPT_DIR/project.env" ]; then
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/project.env"
fi

BOT_IMAGE="${STREAMERBOT_IMAGE:-streamerbot}"
YOUTUBE_SERVICE_NAME="${STREAMERBOT_YOUTUBE_SERVICE:-streamerbot-youtube}"

# Build args every "docker build" in this project must pass: the TeamTalk SDK
# comes from bearware.dk at build time, and go-librespot is version pinned.
IMAGE_BUILD_ARGS=(
    --build-arg "TTSDK_URL_X86_64=${TTSDK_URL_X86_64:-}"
    --build-arg "TTSDK_URL_ARM64=${TTSDK_URL_ARM64:-}"
    --build-arg "GO_LIBRESPOT_VERSION=${GO_LIBRESPOT_VERSION:-0.3.0}"
)
YOUTUBE_BRIDGE_URL="http://127.0.0.1:4417"
UPDATE_LOCK_FILE="/tmp/streamerbot_update.lock"

# ---------------------------------------------------------------------------
# --check-only
#
# Handled before the lock, before any fetch, and before root elevation: this is
# what streamerbot.sh runs at startup. It must be cheap and must never modify
# anything. It prints one line if an update exists, and nothing otherwise.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--check-only" ]; then
    cd "$SCRIPT_DIR" || exit 0
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
    _branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "${STREAMERBOT_BRANCH:-main}")
    _remote=$(timeout 5 git ls-remote origin -h "refs/heads/$_branch" 2>/dev/null | awk '{print $1}' | tr -d '[:space:]')
    _local=$(git rev-parse HEAD 2>/dev/null | tr -d '[:space:]')
    if [ -n "$_remote" ] && [ "$_remote" != "$_local" ]; then
        echo "An update is available. Use Check for updates to install it."
    fi
    exit 0
fi

# Auto-elevate to root via sudo if needed
if [ "$EUID" -ne 0 ]; then
    echo "Not running as root. Re-launching with sudo..."
    exec sudo bash "$0" "$@"
fi

acquire_update_lock() {
    if [ "${STREAMERBOT_UPDATE_LOCK_HELD:-false}" = "true" ] \
        && [ -e "/proc/$$/fd/9" ]; then
        inherited_lock_file=$(readlink -f -- "$UPDATE_LOCK_FILE" 2>/dev/null || true)
        inherited_lock_fd=$(readlink -f -- "/proc/$$/fd/9" 2>/dev/null || true)
        if [ -n "$inherited_lock_file" ] \
            && [ "$inherited_lock_fd" = "$inherited_lock_file" ] \
            && flock -n 9; then
            return 0
        fi
    fi

    unset STREAMERBOT_UPDATE_LOCK_HELD

    exec 9>"$UPDATE_LOCK_FILE"
    if [ "${AUTO_UPDATE:-false}" = "true" ]; then
        if ! flock -n 9; then
            echo "Another update is already in progress."
            return 75
        fi
    else
        echo "Waiting up to 300 seconds for the update lock..."
        if ! flock -w 300 9; then
            echo "Error: Timed out waiting for another update to finish." >&2
            return 75
        fi
    fi

    export STREAMERBOT_UPDATE_LOCK_HELD=true
}


# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function: Display Header
header() {
    clear
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN}      StreamerBot Update Utility          ${NC}"
    echo -e "${GREEN}=========================================${NC}"
    echo ""
}

create_shared_youtube_service() {
    docker rm -f "$YOUTUBE_SERVICE_NAME" >/dev/null 2>&1 || true
    docker create \
        --name "$YOUTUBE_SERVICE_NAME" \
        -p "127.0.0.1:4417:4417" \
        --label "role=streamerbot-infrastructure" \
        --restart always \
        -e "STREAMERBOT_BOTS_ROOT=/bots" \
        -e "YOUTUBE_BRIDGE_HOST=0.0.0.0" \
        -v "${BOTS_ROOT}:/bots:ro" \
        --entrypoint /bin/bash \
        "$BOT_IMAGE" \
        /home/streamer/StreamerBot/youtube_services.sh >/dev/null
}

start_shared_youtube_service() {
    docker start "$YOUTUBE_SERVICE_NAME" >/dev/null
    echo -n "Waiting for shared YouTube service"
    for _ in $(seq 1 60); do
        if curl -fsS "$YOUTUBE_BRIDGE_URL/health" >/dev/null 2>&1; then
            echo -e " [ ${GREEN}OK${NC} ]"
            return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$YOUTUBE_SERVICE_NAME" 2>/dev/null)" != "true" ]; then
            break
        fi
        echo -n "."
        sleep 0.5
    done
    echo -e " [ ${RED}FAILED${NC} ]"
    docker logs --tail 30 "$YOUTUBE_SERVICE_NAME" 2>&1
    return 1
}

shared_youtube_service_supported() {
    docker image inspect "$BOT_IMAGE" >/dev/null 2>&1 \
        && docker run --rm --entrypoint test "$BOT_IMAGE" \
            -f /home/streamer/StreamerBot/youtube_services.sh
}

reconcile_shared_youtube_service() {
    if ! shared_youtube_service_supported; then
        return 0
    fi
    if curl -fsS "$YOUTUBE_BRIDGE_URL/health" >/dev/null 2>&1; then
        return 0
    fi

    echo -e "${YELLOW}Shared YouTube service is unavailable. Recreating it...${NC}"
    create_shared_youtube_service && start_shared_youtube_service
}

# Function: Recreate Bot Containers
recreate_bot_containers() {
    echo -e "${YELLOW}Recreating containers with the new image...${NC}"
    
    if [ ! -d "$BOTS_ROOT" ]; then return; fi
    
    # Get all bot directories
    for d in "$BOTS_ROOT"/*; do
        if [ -d "$d" ]; then
            bot_name=$(basename "$d")
            
            # Remove existing container if it exists
            if [ "$(docker ps -a -q -f name=^/${bot_name}$)" ]; then
                docker rm -f "$bot_name" >/dev/null 2>&1
            fi
            
            # Recreate
            # Ensure cookies.txt exists just in case
            if [ ! -f "$d/cookies.txt" ]; then touch "$d/cookies.txt"; fi
            if [ -f "$d/config.json" ]; then
                tmp_config=$(mktemp)
                jq '.services.yt.cookiefile_path = "data/cookies.txt"' "$d/config.json" > "$tmp_config" && mv "$tmp_config" "$d/config.json"
                chown 1000:1000 "$d/config.json"
            fi
            
            docker create \
                --name "${bot_name}" \
                --network host \
                -e "TTBOT_INSTANCE=${bot_name}" \
                -e "YOUTUBE_BRIDGE_URL=${YOUTUBE_BRIDGE_URL}" \
                --label "role=streamerbot" \
                --restart always \
                -v "${d}:/home/streamer/StreamerBot/data" \
                -v "${d}/cookies.txt:/home/streamer/StreamerBot/data/cookies.txt" \
                "${BOT_IMAGE}" > /dev/null 2>&1
                
            if [ $? -eq 0 ]; then
                echo "  ✓ Container '$bot_name' updated"
            else
                echo "  ✗ Error updating '$bot_name'"
            fi
        fi
    done
}

# Function: Perform Image Rebuild (Internal)
perform_image_rebuild() {
    echo ""
    echo -e "${YELLOW}Starting Image Rebuild...${NC}"
    
    # Capture NAMES of running bots to restart them later
    # We also check a persistent state file in case a previous update was interrupted after stopping bots
    STATE_FILE="/tmp/streamerbot_last_running.txt"
    RUNNING_NAMES=$(docker ps --format "{{.Names}}" -f "label=role=streamerbot")
    
    # Signal running bots that an update has started
    if [ ! -z "$RUNNING_NAMES" ]; then
        echo -e "${YELLOW}Notifying running bots of the update...${NC}"
        echo "$RUNNING_NAMES" | while read -r name; do
            if [ -n "$name" ] && [ -d "$BOTS_ROOT/$name" ]; then
                touch "$BOTS_ROOT/$name/update_in_progress"
            fi
        done
        # Give the bots a brief moment to detect the file and send the warning message
        sleep 1.5
    fi
    
    # Build the image with a commit hash label for version tracking
    CURRENT_HASH=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    echo "Building new image with version label: $CURRENT_HASH"
    docker build "${IMAGE_BUILD_ARGS[@]}" --build-arg CACHEBUST=$(date +%s) --label "commit_hash=$CURRENT_HASH" -t ${BOT_IMAGE} .
    
    if [ $? -eq 0 ]; then
         echo -e "${GREEN}Image built successfully!${NC}"
         
         if [ -z "$RUNNING_NAMES" ] && [ -f "$STATE_FILE" ]; then
             RUNNING_NAMES=$(cat "$STATE_FILE")
             echo -e "${YELLOW}Recovery: Found interrupted update state. Will attempt to restart: $RUNNING_NAMES${NC}"
         fi

         if [ ! -z "$RUNNING_NAMES" ]; then
             echo "$RUNNING_NAMES" > "$STATE_FILE"
             echo -e "${YELLOW}Stopping bots for update...${NC}"
             echo "$RUNNING_NAMES" | xargs docker stop -t 1 > /dev/null 2>&1
         fi
         
         # Recreate containers to use new image
         create_shared_youtube_service || exit 1
         recreate_bot_containers
         start_shared_youtube_service || exit 1
         
         if [ ! -z "$RUNNING_NAMES" ]; then
             echo "$RUNNING_NAMES" | while read -r name; do
                 if [ -n "$name" ] && [ -d "$BOTS_ROOT/$name" ]; then
                     touch "$BOTS_ROOT/$name/update_success"
                 fi
             done
             echo -e "${YELLOW}Restarting active bots...${NC}"
             echo "$RUNNING_NAMES" | xargs docker start > /dev/null 2>&1
             
             # Health Check: Wait for all bots to be confirmed 'running'
             # We wait up to 5 minutes (150 retries * 2s) to accommodate large fleets,
             # but we keep a safety limit to avoid locking the system forever if a bot is broken.
             echo -en "${YELLOW}Verifying bot health (Timeout: 5m)...${NC} "
             MAX_RETRIES=150
             RETRY_COUNT=0
             while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
                 TOTAL_BOTS=$(echo "$RUNNING_NAMES" | wc -l)
                 # grep -xFf matches each name from RUNNING_NAMES individually against running containers
                 STABLE_BOTS=$(docker ps -a --filter "status=running" --filter "status=restarting" --format "{{.Names}}" | grep -xcFf <(echo "$RUNNING_NAMES"))
                 
                 if [ "$STABLE_BOTS" -ge "$TOTAL_BOTS" ]; then
                     echo -e "[ ${GREEN}OK${NC} ] All $TOTAL_BOTS bots are confirmed active."
                     break
                 fi
                 
                 echo -n "."
                 sleep 2
                 RETRY_COUNT=$((RETRY_COUNT + 1))
             done
             
             if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
                 echo -e "\n${RED}Warning: Some bots might have failed to start or crashed.${NC}"
             else
                 # Success! Clear the persistent state file
                 rm -f "$STATE_FILE"
             fi
         fi

         # Clean unused Docker resources & vacuum logs (Option 7 equivalent, but non-interactive)
         echo ""
         echo -e "${YELLOW}🧹 Cleaning up unused Docker resources and system logs...${NC}"
         docker image prune -f
         docker buildx prune -f
         docker builder prune -f
         journalctl --vacuum-time=1d
         echo -e "${GREEN}✓ Cleanup completed!${NC}"
    else
         echo -e "${RED}Error building image!${NC}"
         exit 1
    fi
    sleep 2
}

# Function: Update & Fix Permissions
update_and_fix_permissions() {
    header
    echo -e "${YELLOW} --- Update & Auto-Fix --- ${NC}"
    
    # 1. Determine REAL user
    REAL_USER=${SUDO_USER:-$USER}
    
    if [ "$REAL_USER" == "root" ]; then
         # Fallback 1: Check owner of the script directory
         SCRIPT_OWNER=$(stat -c '%U' "$SCRIPT_DIR")
         if [ "$SCRIPT_OWNER" != "root" ]; then
             REAL_USER="$SCRIPT_OWNER"
         else
             # Fallback 2: Check owner of parent directory
             PARENT_DIR=$(dirname "$SCRIPT_DIR")
             PARENT_OWNER=$(stat -c '%U' "$PARENT_DIR")
             if [ "$PARENT_OWNER" != "root" ]; then
                 REAL_USER="$PARENT_OWNER"
             else
                 # Fallback 3: Use root automatically
                 echo -e "${YELLOW}Could not detect non-root user. Using 'root' automatically.${NC}"
                 REAL_USER="root"
             fi
         fi
    fi

    echo -e "${YELLOW}Target User: ${REAL_USER}${NC}"
    echo ""

    # 2. Check for Updates (GitHub API vs Local Date)
    REPO_OWNER="${STREAMERBOT_REPO_OWNER:-}"
    REPO_NAME="${STREAMERBOT_REPO_NAME:-StreamerBot}"
    BRANCH="${STREAMERBOT_BRANCH:-main}"

    if [ -z "$REPO_OWNER" ]; then
        echo -e "${RED}Error. STREAMERBOT_REPO_OWNER is not set in project.env, so updates cannot run.${NC}"
        echo "Open project.env and set STREAMERBOT_REPO_OWNER to your GitHub username, or run streamerbot.sh, which will ask for it."
        return 1 2>/dev/null || exit 1
    fi
    
    echo -e "${YELLOW}Checking for updates...${NC}"
    
    # Check if we are in a git repository
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        # Fetch remote info
        git fetch origin "$BRANCH" -q
        REMOTE_HASH=$(git rev-parse "origin/$BRANCH" | tr -d '[:space:]')
        LOCAL_HASH=$(git rev-parse HEAD | tr -d '[:space:]')
        
        # Check running version
        # Use 'tr -d' to ensure no weird whitespace/newlines break the comparison
        RUNNING_HASH=$(docker inspect ${BOT_IMAGE} --format '{{ index .Config.Labels "commit_hash" }}' 2>/dev/null | tr -d '[:space:]')
        if [ -z "$RUNNING_HASH" ] || [ "$RUNNING_HASH" = "<novalue>" ] || [ "$RUNNING_HASH" = "<noopt>" ] || [[ "$RUNNING_HASH" == *"<no"* ]]; then
            RUNNING_HASH="none"
        fi
        
        # Basic State Detection (Image and Directories)
        IMAGE_EXISTS=$(docker images -q ${BOT_IMAGE} 2>/dev/null)
        
        if [ -z "$IMAGE_EXISTS" ] && [ ! -d "$BOTS_ROOT" ] && [ ! -f "$SCRIPT_DIR/config.json" ]; then
            IS_FIRST_INSTALL=true
        else
            IS_FIRST_INSTALL=false
        fi

        # Determine if we need an update or a rebuild
        NEEDS_PULL=false
        NEEDS_REBUILD=false
        IS_BEHIND=false
        
        # Check for uncommitted local changes
        LOCAL_CHANGES=$(git status --porcelain)
        HAS_LOCAL_CHANGES=false
        if [ -n "$LOCAL_CHANGES" ]; then
            HAS_LOCAL_CHANGES=true
        fi
        
        # Check if local is behind remote
        if [ "$REMOTE_HASH" != "$LOCAL_HASH" ]; then
            if git merge-base --is-ancestor "$LOCAL_HASH" "$REMOTE_HASH"; then
                IS_BEHIND=true
                NEEDS_PULL=true
                echo -e "${YELLOW}Local version is behind remote.${NC}"
            else
                echo -e "${YELLOW}Local version has diverged or is ahead of remote. Auto-pull skipped to protect local changes.${NC}"
            fi
        fi

        if [ "$HAS_LOCAL_CHANGES" = true ]; then
             echo -e "${RED}Warning: You have uncommitted local changes!${NC}"
             # Don't block the pull - git reset --hard will handle local changes.
             # The user will be prompted for confirmation in manual mode.
        fi
        
        if [ "$LOCAL_HASH" != "$RUNNING_HASH" ]; then
            NEEDS_REBUILD=true
        fi
        
        if [ "$NEEDS_PULL" = true ] || [ "$NEEDS_REBUILD" = true ] || [ "$IS_BEHIND" = true ]; then
            if [ "$IS_FIRST_INSTALL" = "true" ]; then
                echo -e "${GREEN}Initial Setup / Installation Required!${NC}"
            else
                echo -e "${GREEN}Update or Version mismatch found!${NC}"
                if [ "$RUNNING_HASH" == "none" ]; then
                    echo -e "${YELLOW}Note: Running image exists but is missing version label.${NC}"
                fi
            fi
            echo "Remote:  $REMOTE_HASH"
            echo "Local:   $LOCAL_HASH"
            echo "Running: $RUNNING_HASH"
            UPDATE_FOUND=true
            [ "$NEEDS_REBUILD" = true ] && REBUILD_REQUIRED=true
            # If behind remote, always trigger rebuild after pull
            [ "$IS_BEHIND" = true ] && REBUILD_REQUIRED=true
        else
            echo -e "${GREEN}Already up to date and running latest version ($LOCAL_HASH).${NC}"
            UPDATE_FOUND=false
        fi
    else
        # Basic State Detection (Image and Directories)
        IMAGE_EXISTS=$(docker images -q ${BOT_IMAGE} 2>/dev/null)
        
        if [ -z "$IMAGE_EXISTS" ] && [ ! -d "$BOTS_ROOT" ] && [ ! -f "$SCRIPT_DIR/config.json" ]; then
            IS_FIRST_INSTALL=true
        else
            IS_FIRST_INSTALL=false
        fi

        # Fallback to date-based check if not a git repo yet (first install)
        LATEST_COMMIT_DATE=$(curl -s "https://api.github.com/repos/$REPO_OWNER/$REPO_NAME/commits/$BRANCH" | jq -r '.commit.committer.date')
        
        if [ -n "$LATEST_COMMIT_DATE" ] && [ "$LATEST_COMMIT_DATE" != "null" ]; then
            REMOTE_TS=$(date -d "$LATEST_COMMIT_DATE" +%s)
            LOCAL_TS=$(stat -c %Y "$SCRIPT_DIR/streamerbot.sh" 2>/dev/null || echo 0)
            
            if [ "$REMOTE_TS" -gt "$LOCAL_TS" ] || [ "$IS_FIRST_INSTALL" = "true" ]; then
                if [ "$IS_FIRST_INSTALL" = "true" ]; then
                    echo -e "${GREEN}Initial Setup / Installation Required!${NC}"
                else
                    echo -e "${GREEN}Update found (date-based)!${NC}"
                fi
                UPDATE_FOUND=true
            else
                echo -e "${GREEN}Already up to date (date-based).${NC}"
                UPDATE_FOUND=false
            fi
        else
            # If API fails, assume we might need update if we aren't a git repo
            if [ "$IS_FIRST_INSTALL" = "true" ]; then
                echo -e "${YELLOW}API check failed but no installation found. Proceeding with Setup.${NC}"
                UPDATE_FOUND=true
            else
                echo -e "${RED}Warning: Could not check updates via API (rate limit?).${NC}"
                UPDATE_FOUND=false
            fi
        fi
    fi

    UPDATE_PERFORMED=false
    
    if [ "$UPDATE_FOUND" == "true" ]; then
        echo ""
        if [ "$IS_FIRST_INSTALL" == "true" ]; then
            echo "This will:"
            echo "1. Clone/pull the latest repository code"
            echo "2. Build and setup the StreamerBot Docker image"
            echo "3. Initialize environment and fix permissions"
        else
            echo "This will:"
            echo "1. Backup 'bots' folder (configs/cookies)"
            echo "2. Clone/pull the latest repository code"
            echo "3. Update all local files"
            echo "4. Restore backup"
        fi
        echo ""
        
        if [ "$AUTO_UPDATE" = "true" ]; then
            echo "Auto-Update mode detected. Proceeding automatically..."
            confirm_update="y"
        elif [ "$IS_FIRST_INSTALL" = "true" ]; then
            echo -e "${GREEN}First installation detected. Proceeding automatically...${NC}"
            confirm_update="y"
        elif [ "${STREAMERBOT_UPDATE_REEXECED:-false}" = "true" ]; then
            echo -e "${YELLOW}Continuing update with the refreshed updater...${NC}"
            confirm_update="y"
        elif [ -z "$IMAGE_EXISTS" ]; then
            # No Docker image exists at all — rebuild automatically regardless of local changes
            if [ "$HAS_LOCAL_CHANGES" = true ]; then
                echo -e "${YELLOW}No Docker image found. Local changes detected but proceeding automatically to build image...${NC}"
            else
                echo -e "${YELLOW}Docker image missing. Rebuilding automatically...${NC}"
            fi
            confirm_update="y"
        else
            echo -e "${YELLOW}Local changes detected: ${HAS_LOCAL_CHANGES}${NC}"
            if [ "$HAS_LOCAL_CHANGES" = true ]; then
                echo -e "${RED}WARNING: Proceeding will OVERWRITE your local uncommitted changes!${NC}"
                read -p "Do you REALLY want to overwrite your changes and update? [y/N]: " confirm_update
            else
                read -p "Update found. Do you want to proceed? [y/N]: " confirm_update
            fi
        fi
            
        if [[ "$confirm_update" =~ ^[yY]$ ]]; then
                echo -e "${YELLOW}Starting update...${NC}"
                
                # Check if we are in a git repository
                if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
                    echo "Performing forced synchronization with GitHub..."
                    # Backup configs before sync just in case
                    TMP_BACKUP=$(mktemp -d)
                    if [ -d "$BOTS_ROOT" ]; then 
                        mkdir -p "$TMP_BACKUP/bots"
                        cp -r "$BOTS_ROOT/." "$TMP_BACKUP/bots/"
                    fi
                    
                    # Force synchronization to match origin exactly
                    git fetch origin "$BRANCH"
                    git reset --hard "origin/$BRANCH"
                    git clean -fd # Also remove untracked files that might conflict
                    
                    # Restore backup if needed
                    if [ -d "$TMP_BACKUP/bots" ]; then 
                        cp -rf "$TMP_BACKUP/bots/." "$BOTS_ROOT/" 2>/dev/null
                    fi
                    rm -rf "$TMP_BACKUP"
                    
                    UPDATE_PERFORMED=true
                else
                    # First time conversion to git repo or standalone install
                    # Define Temp Dirs
                    TMP_DIR=$(mktemp -d)
                    BACKUP_DIR="$TMP_DIR/backup"
                    mkdir -p "$BACKUP_DIR"
                    
                    # 1. Backup Configs
                    echo "Backing up configurations..."
                    if [ -d "$BOTS_ROOT" ]; then cp -r "$BOTS_ROOT" "$BACKUP_DIR/"; fi
                    
                    # 2. Clone Repository
                    echo "Cloning repository..."
                    CLONE_DIR="$TMP_DIR/clone"
                    git clone "https://github.com/$REPO_OWNER/$REPO_NAME.git" "$CLONE_DIR"
                    
                    if [ $? -eq 0 ]; then
                        echo "Installing..."
                        cp -rf "$CLONE_DIR/." "$SCRIPT_DIR/"
                        
                        # 4. Restore Backup
                        if [ -d "$BACKUP_DIR/bots" ]; then cp -rf "$BACKUP_DIR/bots/"* "$BOTS_ROOT/" 2>/dev/null; fi
                        
                        UPDATE_PERFORMED=true
                        rm -rf "$TMP_DIR"
                    else
                        echo -e "${RED}Clone failed.${NC}"
                    fi
                fi

                if [ "$UPDATE_PERFORMED" == "true" ]; then
                     # Update timestamp
                     touch "$SCRIPT_DIR/streamerbot.sh"
                     echo -e "${GREEN}Update applied!${NC}"
                     if [ "${STREAMERBOT_UPDATE_REEXECED:-false}" != "true" ]; then
                         echo -e "${YELLOW}Reloading the updated deployment logic...${NC}"
                         export STREAMERBOT_UPDATE_REEXECED=true
                         exec bash "$SCRIPT_DIR/update.sh" "$@"
                         reexec_status=$?
                         echo -e "${RED}Failed to reload the updated deployment logic.${NC}" >&2
                         return "$reexec_status"
                     fi
                fi
            else
                echo "Update cancelled."
                exit 0
            fi
        fi
    if [ "$UPDATE_PERFORMED" == "true" ] || [ "$REBUILD_REQUIRED" == "true" ]; then
        # The TeamTalk SDK is no longer downloaded here. It is fetched from
        # bearware.dk inside the Docker image build using the URLs in
        # project.env, so the host never needs a TeamTalk_DLL directory.
        # Remove any left over from a pre-5.22 install.
        if [ -d "TeamTalk_DLL" ] || [ -f "TeamTalk_DLL.zip" ]; then
            echo "Removing the old host-side TeamTalk_DLL. The SDK now ships inside the image."
            rm -rf TeamTalk_DLL TeamTalk_DLL.zip
        fi

        echo ""
        echo -e "${YELLOW}Fixing permissions...${NC}"
        
        # 4. Fix permissions
        # Operate on SCRIPT_DIR
        TARGET_FIX_DIR="$SCRIPT_DIR"
        TARGET_FIX_DIR=$(realpath "$TARGET_FIX_DIR")
        
        echo "Setting ownership to $REAL_USER:$REAL_USER for $TARGET_FIX_DIR..."
        chown -R "$REAL_USER":"$REAL_USER" "$TARGET_FIX_DIR"
        
        echo "Setting permissions (777 - Full Control)..."
        chmod -R 777 "$TARGET_FIX_DIR"
        
        chmod +x "$TARGET_FIX_DIR"/*.sh 2>/dev/null
        
        echo ""
        echo -e "${GREEN}Done! Permissions set to User: $REAL_USER, Mode: 777.${NC}"
    fi
    
    # 5. Auto-Rebuild (if update occurred or version mismatch detected)
    if [ "$UPDATE_PERFORMED" == "true" ] || [ "$REBUILD_REQUIRED" == "true" ]; then
        echo ""
        echo -e "${YELLOW}Update applied or version mismatch detected. Rebuilding Docker image...${NC}"
        # Wait a bit
        sleep 2
        perform_image_rebuild
    fi
    
    # Return to script dir
    cd "$SCRIPT_DIR" || return
}

# Function: Configure Auto-Updater Service
configure_auto_updater() {
    # Check if masked - respect user choice to disable
    if LANG=C systemctl list-unit-files streamerbot-updater.service 2>/dev/null | grep -q "masked"; then
        echo -e "${YELLOW}Auto-Updater is currently masked. Skipping configuration to respect manual override.${NC}"
        return
    fi

    echo ""
    echo -e "${YELLOW} --- Configuring Auto-Updater Service --- ${NC}"
    
    SERVICE_FILE="/etc/systemd/system/streamerbot-updater.service"
    
    # Create the service file
    echo "Creating systemd service..."
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=StreamerBot Auto-Updater Watcher
After=network.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=/bin/bash $SCRIPT_DIR/auto_updater.sh
Restart=always
User=root

[Install]
WantedBy=multi-user.target
EOF

    # Fix permissions for the watcher script
    chmod +x "$SCRIPT_DIR/auto_updater.sh"

    # Reload systemd and enable service
    echo "Enabling and starting service..."
    systemctl daemon-reload
    systemctl enable streamerbot-updater.service >/dev/null 2>&1
    
    # Only restart if not being called by the auto-updater to avoid killing our own process
    if [ "$AUTO_UPDATE" != "true" ]; then
        systemctl restart --no-block streamerbot-updater.service
        echo -e "${GREEN}Auto-Updater Service configured and restarting in background!${NC}"
    else
        echo -e "${GREEN}Auto-Updater Service configured (restart skipped to avoid interruption).${NC}"
    fi
}
# Run
install_deps_light() {
    if ! command -v jq &> /dev/null; then apt-get install -y jq; fi
    if ! command -v git &> /dev/null; then apt-get install -y git; fi
    if ! command -v curl &> /dev/null; then apt-get install -y curl; fi
    if ! command -v unzip &> /dev/null; then apt-get install -y unzip; fi
}

# --- MAIN EXECUTION WRAPPER ---
# Wrapping in a main function ensures bash loads the entire block into memory
# protecting against crashes if the script modifies itself mid-execution during git reset.
main() {
    acquire_update_lock || return $?
    install_deps_light || return $?
    update_and_fix_permissions "$@" || return $?

    # A legacy updater can replace this script while continuing with its old
    # in-memory functions. Reconcile infrastructure even when no rebuild remains.
    reconcile_shared_youtube_service || return $?
    
    # The user mandated that service configuration MUST run every time
    # but not block the flow (implemented via --no-block inside the function).
    configure_auto_updater || return $?
}

# Execute main in memory
main "$@"
