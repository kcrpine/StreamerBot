#!/bin/bash

# Auto-detect script location and set paths dynamically
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOTS_ROOT="${SCRIPT_DIR}/bots"
CONFIG_SOURCE="config.json"

# Shared project constants (repository, image name, TeamTalk SDK URLs,
# go-librespot version, update interval).
if [ -f "$SCRIPT_DIR/project.env" ]; then
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/project.env"
fi

# Configuration
BOT_IMAGE="${STREAMERBOT_IMAGE:-streamerbot}"
YOUTUBE_SERVICE_NAME="${STREAMERBOT_YOUTUBE_SERVICE:-streamerbot-youtube}"
YOUTUBE_BRIDGE_URL="http://127.0.0.1:4417"

# Build args every "docker build" must pass: the TeamTalk SDK is downloaded
# from bearware.dk during the build, and go-librespot is version pinned.
IMAGE_BUILD_ARGS=(
    --build-arg "TTSDK_URL_X86_64=${TTSDK_URL_X86_64:-}"
    --build-arg "TTSDK_URL_ARM64=${TTSDK_URL_ARM64:-}"
    --build-arg "GO_LIBRESPOT_VERSION=${GO_LIBRESPOT_VERSION:-0.9.1}"
)

# Auto-elevate to root via sudo if needed
# --help needs no privileges, so answer it before elevating. Asking someone for a
# root password to read a help text is a bad trade.
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    echo "StreamerBot manager"
    echo ""
    echo "Run with no arguments for the menu, or use one of these:"
    echo ""
    echo "  --status         List every bot and whether it is running."
    echo "  --services       Show the shared YouTube service and the image."
    echo "  --start-all      Start every bot."
    echo "  --stop-all       Stop every bot."
    echo "  --restart-all    Restart every bot."
    echo "  --check-updates  Say whether an update is available, without installing it."
    echo "  --logs NAME      Show the last 50 log lines for one bot."
    echo "  --help           This text."
    echo ""
    echo "Everything except --help needs root, and will ask for it."
    exit 0
fi

# Validate the flag name before elevating, so a typo does not cost a password
# prompt first.
case "${1:-}" in
    ""|--status|--services|--start-all|--stop-all|--restart-all|--check-updates|--logs)
        ;;
    *)
        echo "Error. Unknown option: $1"
        echo "Run with --help to see the available options."
        exit 1
        ;;
esac

if [ "$EUID" -ne 0 ]; then
    echo "Not running as root. Re-launching with sudo..."
    exec sudo bash "$0" "$@"
fi


# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ---------------------------------------------------------------------------
# Manager log.
#
# Creating a bot touches Docker, jq, the filesystem and the network, and when it
# goes wrong the useful detail has usually scrolled away or was never printed. So
# every step is appended here with a timestamp, whether or not it is also shown.
#
# The log lives outside bots/ on purpose: a creation that fails early may never
# get a bot directory, and that is exactly the case worth having a record of.
# Nothing secret is written -- passwords and channel passwords are never passed
# to it, only the fact that a step ran and what it returned.
# ---------------------------------------------------------------------------
MANAGER_LOG="${SCRIPT_DIR}/logs/manager.log"

log_line() {
    local message="$1"
    mkdir -p "$(dirname "$MANAGER_LOG")" 2>/dev/null || return 0
    printf '%s  %s
' "$(date '+%Y-%m-%d %H:%M:%S')" "$message" >> "$MANAGER_LOG" 2>/dev/null || true
}

# Say it and record it. Used for the steps a user should see anyway.
log_say() {
    echo "$1"
    log_line "$1"
}

# Run a command, record its exit status and its output, and show the output only
# when it failed. Keeps a successful creation quiet without losing the detail.
log_run() {
    local description="$1"
    shift
    local output status
    output=$("$@" 2>&1)
    status=$?
    log_line "$description -> exit $status"
    if [ -n "$output" ]; then
        printf '%s
' "$output" | while IFS= read -r line; do
            log_line "    $line"
        done
    fi
    if [ "$status" -ne 0 ]; then
        echo "Error. $description failed."
        [ -n "$output" ] && printf '%s
' "$output" | tail -5
        echo "Full detail is in $MANAGER_LOG"
    fi
    return "$status"
}

# Function: Display Header
header() {
    # Deliberately does not call clear. Scrollback is what a screen reader user
    # relies on to review what just happened, and clearing it destroys the only
    # record of the last action's output.
    echo ""
    echo "StreamerBot manager"
    echo ""
}



# Function: Install Dependencies
install_dependencies() {
    header
    echo -e "${YELLOW}Checking dependencies...${NC}"

    if ! command -v docker &> /dev/null; then
        echo "Docker not found. Installing via official script..."
        curl -fsSL https://get.docker.com -o get-docker.sh
        sh get-docker.sh
        rm -f get-docker.sh
        
        if command -v systemctl &> /dev/null; then
            systemctl enable --now docker
        else
            echo -e "${YELLOW}Warning: systemctl not found. Please ensure the Docker daemon is started manually.${NC}"
        fi
        
        REAL_USER=${SUDO_USER:-$USER}
        if [ "$REAL_USER" != "root" ]; then
            usermod -aG docker "$REAL_USER"
            echo "User '$REAL_USER' added to the docker group."
        fi
    else
        echo -e "${GREEN}Docker is already installed.${NC}"
    fi

    if ! command -v jq &> /dev/null; then
        echo "jq not found. Installing..."
        if command -v apt-get &> /dev/null; then
            apt-get update && apt-get install -y jq
        elif command -v dnf &> /dev/null; then
            dnf install -y jq
        elif command -v yum &> /dev/null; then
            yum install -y jq
        elif command -v pacman &> /dev/null; then
            pacman -S --noconfirm jq
        elif command -v zypper &> /dev/null; then
            zypper install -y jq
        elif command -v apk &> /dev/null; then
            apk add --no-cache jq
        else
            echo -e "${RED}Please install 'jq' manually.${NC}"
        fi
    else
        echo -e "${GREEN}jq is already installed.${NC}"
    fi
    sleep 1
}

# Run dependencies check immediately
install_dependencies


create_shared_youtube_service() {
    docker rm -f "$YOUTUBE_SERVICE_NAME" >/dev/null 2>&1 || true
    docker create \
        --name "$YOUTUBE_SERVICE_NAME" \
        -p "127.0.0.1:4417:4417" \
        --label "role=streamerbot-infrastructure" \
        --restart always \
        -e "STREAMERBOT_BOTS_ROOT=/bots" \
        -e "YOUTUBE_BRIDGE_HOST=0.0.0.0" \
        -v "${BOTS_ROOT}:/bots:rw" \
        --entrypoint /bin/bash \
        "$BOT_IMAGE" \
        /home/streamer/StreamerBot/youtube_services.sh >/dev/null
}

start_shared_youtube_service() {
    docker start "$YOUTUBE_SERVICE_NAME" >/dev/null
    echo "Waiting for the shared YouTube service to start."
    for _ in $(seq 1 60); do
        if curl -fsS "$YOUTUBE_BRIDGE_URL/health" >/dev/null 2>&1; then
            echo "OK. The shared YouTube service is ready."
            return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$YOUTUBE_SERVICE_NAME" 2>/dev/null)" != "true" ]; then
            break
        fi
        sleep 0.5
    done
    echo "Error. The shared YouTube service did not become ready."
    docker logs --tail 30 "$YOUTUBE_SERVICE_NAME" 2>&1
    return 1
}

ensure_bot_data_ownership() {
    # Every bot directory belongs to uid 1000, the container user. The shared
    # YouTube bridge creates <bot>/youtube_auth/ to store that bot's OAuth
    # tokens, so the directory has to be writable by that uid or sign-in fails
    # with EACCES and the bot silently stays anonymous.
    #
    # Modes are deliberately tight: these directories hold credentials.
    [ -d "$BOTS_ROOT" ] || return 0

    local bot_dir
    for bot_dir in "$BOTS_ROOT"/*; do
        [ -d "$bot_dir" ] || continue
        mkdir -p "$bot_dir/youtube_auth"
        chown -R 1000:1000 "$bot_dir" 2>/dev/null || true
        chmod 700 "$bot_dir/youtube_auth" 2>/dev/null || true
    done
}

shared_youtube_mount_is_current() {
    local bot_dir bot_name mount_rw

    # The bridge writes each bot's YouTube OAuth tokens to
    # /bots/<bot>/youtube_auth/, so the mount has to be read-write. Containers
    # created before that change mounted it read-only, and checking only that
    # the directory is visible would let one of those survive forever: it would
    # pass this test, then silently fail to save a single token.
    mount_rw=$(docker inspect -f \
        '{{range .Mounts}}{{if eq .Destination "/bots"}}{{.RW}}{{end}}{{end}}' \
        "$YOUTUBE_SERVICE_NAME" 2>/dev/null)
    if [ "$mount_rw" != "true" ]; then
        return 1
    fi

    for bot_dir in "$BOTS_ROOT"/*; do
        [ -d "$bot_dir" ] || continue
        bot_name=$(basename "$bot_dir")
        docker exec "$YOUTUBE_SERVICE_NAME" test -d "/bots/$bot_name"
        return
    done

    return 0
}

ensure_shared_youtube_service() {
    if ! docker inspect "$YOUTUBE_SERVICE_NAME" >/dev/null 2>&1; then
        create_shared_youtube_service || return 1
    fi
    start_shared_youtube_service || return 1

    if ! shared_youtube_mount_is_current; then
        echo -e "${YELLOW}Shared YouTube service has a stale bots mount; recreating it...${NC}"
        create_shared_youtube_service || return 1
        start_shared_youtube_service
    fi
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
            
            # Make sure the credential directories exist. Nothing writes a
            # cookies.txt any more, and cookiefile_path is no longer forced back
            # into the config: the bridge stopped reading it in Phase 2.
            mkdir -p "$d/secrets" "$d/browser" "$d/youtube_auth" "$d/librespot"
            chown -R 1000:1000 "$d" 2>/dev/null || true
            chmod 700 "$d/secrets" "$d/youtube_auth" "$d/librespot" 2>/dev/null || true
            
            docker create \
                --name "${bot_name}" \
                --network host \
                -e "TTBOT_INSTANCE=${bot_name}" \
                -e "YOUTUBE_BRIDGE_URL=${YOUTUBE_BRIDGE_URL}" \
                --label "role=streamerbot" \
                --restart always \
                -v "${d}:/home/streamer/StreamerBot/data" \
                "${BOT_IMAGE}" > /dev/null 2>&1
                
            if [ $? -eq 0 ]; then
                echo "  ✓ Container '$bot_name' updated"
            else
                echo "  ✗ Error updating '$bot_name'"
            fi
        fi
    done
}

# Function: Build Docker Image
build_image() {
    header
    echo -e "${YELLOW}Checking Docker image '${BOT_IMAGE}'...${NC}"
    
    # Check if Dockerfile exists in current directory
    if [ ! -f "Dockerfile" ]; then
        echo -e "${RED}Error: Dockerfile not found in current directory!${NC}"
        echo "Please run this script in the folder where the StreamerBot Dockerfile is located."
        exit 1
    fi

    if [[ "$(docker images -q ${BOT_IMAGE} 2> /dev/null)" == "" ]]; then
        echo "Image not found. Building image..."
        CURRENT_HASH=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
        docker build "${IMAGE_BUILD_ARGS[@]}" --build-arg CACHEBUST=$(date +%s) --label "commit_hash=$CURRENT_HASH" -t ${BOT_IMAGE} .
        if [ $? -eq 0 ]; then
             echo -e "${GREEN}Image built successfully!${NC}"
        else
             echo -e "${RED}Error building image! Check the Dockerfile.${NC}"
             exit 1
        fi
        sleep 2
    else
        echo -e "${GREEN}Image '${BOT_IMAGE}' already exists.${NC}"
        # No prompt here anymore
        sleep 1
    fi
}

# Function: Force Rebuild Image (Menu Option)
force_rebuild_image() {
    header
    echo -e "${YELLOW} --- Rebuild Image / Update Code --- ${NC}"
    echo "This will pull the latest changes (if you updated files) and rebuild the Docker image."
    echo ""
    read -p "Are you sure? (y/N): " confirm
    
    if [[ ! "$confirm" =~ ^[yY]$ ]]; then
        return
    fi

    echo ""
    echo "Checking running bots..."
    # Capture NAMES of running bots to restart them later
    RUNNING_NAMES=$(docker ps --format "{{.Names}}" -f "label=role=streamerbot")
    
    echo -e "${YELLOW}Building new image (updating code and PIP libraries)...${NC}"
    CURRENT_HASH=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    docker build "${IMAGE_BUILD_ARGS[@]}" --build-arg CACHEBUST=$(date +%s) --label "commit_hash=$CURRENT_HASH" -t ${BOT_IMAGE} .
    
    if [ $? -eq 0 ]; then
         echo -e "${GREEN}Image updated successfully!${NC}"
         
         if [ ! -z "$RUNNING_NAMES" ]; then
             echo -e "${YELLOW}Stopping bots for update...${NC}"
             echo "$RUNNING_NAMES" | xargs docker stop -t 1 > /dev/null 2>&1
         fi
         
         # Recreate containers to use new image
         create_shared_youtube_service || return
         recreate_bot_containers
         start_shared_youtube_service || return
         
         if [ ! -z "$RUNNING_NAMES" ]; then
             echo -e "${YELLOW}Restarting active bots...${NC}"
             echo "$RUNNING_NAMES" | xargs docker start > /dev/null 2>&1
             echo -e "${GREEN}Bots restarted with the new code.${NC}"
         fi
    else
         echo -e "${RED}Error building image!${NC}"
         # Don't exit script, just return to menu
         read -p "Press Enter to continue..."
         return
    fi
    
    read -p "Process completed. Press Enter to return..."
}

# Function: Create Bot
create_bot() {
    header
    echo -e "${YELLOW} --- Create New Bot --- ${NC}"
    
    if [ ! -f "$CONFIG_SOURCE" ]; then
       echo -e "${RED}Error: File '$CONFIG_SOURCE' not found in current directory.${NC}"
       return
    fi
    
    read -p "Bot Name (will be the folder name and container name): " bot_name
    if [[ -z "$bot_name" ]]; then echo -e "${RED}Invalid name.${NC}"; sleep 2; return; fi
    
    BOT_DIR="${BOTS_ROOT}/${bot_name}"
    
    # Check if container with this name exists
    if [ "$(docker ps -a -q -f name=^/${bot_name}$)" ]; then
        echo -e "${RED}Error: A container with the name '${bot_name}' already exists.${NC}"
        sleep 2
        return
    fi
    
    if [ -d "$BOT_DIR" ]; then
        echo -e "${RED}A folder for this bot already exists!${NC}"
        sleep 2
        return
    fi

    # ... inputs ...
    read -p "TeamTalk Server Address: " server_addr
    read -p "TCP Port (Default 10333): " tcp_port
    tcp_port=${tcp_port:-10333}
    read -p "UDP Port (Default 10333): " udp_port
    udp_port=${udp_port:-10333}
    
    echo "Encrypted?"
    echo "1. No (False)"
    echo "2. Yes (True)"
    read -p "Option: " encrypted_opt
    if [ "$encrypted_opt" == "2" ]; then encrypted="true"; else encrypted="false"; fi
    
    read -p "Username: " username
    read -sp "Password: " password
    echo ""
    read -p "Bot Nickname (Default: StreamerBot): " nickname
    nickname=${nickname:-StreamerBot}

    # No cookies question. YouTube signs in with a device code once the bot
    # is running, so there is nothing useful to ask for, and asking implied a
    # file was required.
    cookies_path=""

    # Startup commands: how a bot plays a stream the moment it connects. It is
    # easy to miss that the setting exists at all, so it is asked for here
    # rather than left to be discovered in a config file.
    echo ""
    echo "A startup command runs every time the bot connects. The usual one plays"
    echo "a stream, for example:  u http://example.org:8000/live.mp3"
    read -p "Startup command (Enter for none): " start_command
    
    read -p "Channel (Default: /): " channel
    channel=${channel:-/}
    read -sp "Channel Password (Default: empty): " channel_password
    echo ""

    # --- Delete timer ---
    current_del_timer=$(jq -r '.general.delete_uploaded_files_after // 300' "$CONFIG_SOURCE" 2>/dev/null)
    echo ""
    echo -e "${YELLOW}Delete uploaded files after how many seconds?${NC}"
    echo "  Current default in config.json: ${current_del_timer}s"
    echo "  (0 = never delete)"
    read -p "Timer in seconds [Default: ${current_del_timer}]: " delete_timer
    delete_timer=${delete_timer:-$current_del_timer}
    if [[ ! "$delete_timer" =~ ^[0-9]+$ ]]; then
        echo -e "${RED}Invalid value. Using default (${current_del_timer}).${NC}"
        delete_timer=$current_del_timer
    fi
    echo ""

    
    # Batch create option - ask BEFORE creating
    echo ""
    read -p "Batch create? (y/N): " batch_create
    additional_bots=0
    nickname_base=""
    container_base=""
    
    if [[ "$batch_create" =~ ^[yY]$ ]]; then
        read -p "How many ADDITIONAL bots to create (besides the main one)?: " additional_bots
        if [[ ! "$additional_bots" =~ ^[0-9]+$ ]] || [ "$additional_bots" -lt 0 ]; then
            echo -e "${RED}Invalid quantity. Creating only the main bot.${NC}"
            additional_bots=0
        fi
        
        if [ "$additional_bots" -gt 0 ]; then
            echo -e "${YELLOW}WARNING: Use a different BASE name for containers to avoid conflicts!${NC}"
            read -p "BASE name for CONTAINERS (Enter = default 'bot'): " container_base
            if [[ -z "$container_base" ]]; then
                container_base="bot"
            fi
            read -p "BASE name for NICKNAMES (Enter = same as container '$container_base'): " nickname_base
            if [[ -z "$nickname_base" ]]; then
                nickname_base="$container_base"
            fi
        else
            # No additional bots, use bot_name as container_base
            container_base="$bot_name"
        fi
    else
        # Single bot creation - use bot_name as container_base
        container_base="$bot_name"
    fi
    
    total_bots=$((additional_bots + 1))
    echo -e "${YELLOW}Creating $total_bots bot(s)...${NC}"
    
    
    # Find highest existing number for both container names and nicknames
    highest_num=0
    base_name_exists=false
    highest_nickname_num=0
    nickname_base_exists=false
    
    if [ -d "$BOTS_ROOT" ] && [ -n "$container_base" ]; then
        for d in "$BOTS_ROOT"/*; do
            if [ -d "$d" ]; then
                name=$(basename "$d")
                
                # Check container names strictly against container_base
                if [[ "$name" == "$container_base" ]]; then
                    base_name_exists=true
                elif [[ "$name" =~ ^${container_base}([0-9]+)$ ]]; then
                    num="${BASH_REMATCH[1]}"
                    [ "$num" -gt "$highest_num" ] && highest_num=$num
                fi
                
                # Check nicknames in config.json strictly against nickname_base
                # BUT ONLY for bots on the SAME SERVER (hostname + port)
                config_file="$d/config.json"
                if [ -f "$config_file" ] && [ -n "$nickname_base" ]; then
                    # Get server info from this bot's config
                    existing_hostname=$(jq -r '.teamtalk.hostname // ""' "$config_file")
                    existing_tcp_port=$(jq -r '.teamtalk.tcp_port // 0' "$config_file")
                    
                    # Only check nicknames if it's the SAME server
                    if [[ "$existing_hostname" == "$server_addr" ]] && [[ "$existing_tcp_port" == "$tcp_port" ]]; then
                        existing_nickname=$(jq -r '.teamtalk.nickname // ""' "$config_file")
                        
                        if [[ "$existing_nickname" == "$nickname_base" ]]; then
                            nickname_base_exists=true
                        elif [[ "$existing_nickname" =~ ^${nickname_base}([0-9]+)$ ]]; then
                            nick_num="${BASH_REMATCH[1]}"
                            [ "$nick_num" -gt "$highest_nickname_num" ] && highest_nickname_num=$nick_num
                        fi
                    fi
                fi
            fi
        done
    fi
    
    # Simple sequential counter for naming
    # When base doesn't exist, we start numbering from 1 (bot, bot1, bot2...)
    # When base exists, we continue from highest_num + 1
    if [ "$base_name_exists" == "true" ]; then
        next_container_num=$((highest_num + 1))
    else
        next_container_num=1
    fi
    
    if [ "$nickname_base_exists" == "true" ]; then
        next_nickname_num=$((highest_nickname_num + 1))
    else
        next_nickname_num=1
    fi
    
    # Track if we've used the base name yet
    container_base_used=$base_name_exists
    nickname_base_used=$nickname_base_exists
    
    # Loop to create bots
    for i in $(seq 1 $total_bots); do
        # Determine container name
        if [ $i -eq 1 ]; then
            # First bot uses the explicit name provided by user
            current_bot_name="$bot_name"
            current_nickname="$nickname"
            
            # If the chosen name happens to be the same as container_base, mark it as used
            if [ "$current_bot_name" == "$container_base" ]; then
                container_base_used=true
            fi
            
            # If the chosen nickname happens to be the same as nickname_base, mark it as used
            if [ -n "$nickname_base" ] && [ "$current_nickname" == "$nickname_base" ]; then
                nickname_base_used=true
            fi
        else
            # Additional bots use container_base for container naming
            # Sequence: bot, bot1, bot2, bot3...
            if [ "$container_base_used" == "false" ]; then
                # Base name not used yet, use it now
                current_bot_name="$container_base"
                container_base_used=true
            else
                # Base name already used, use numbered version
                current_bot_name="${container_base}${next_container_num}"
                next_container_num=$((next_container_num + 1))
            fi
            
            # Nickname follows same logic
            if [ -n "$nickname_base" ]; then
                if [ "$nickname_base_used" == "false" ]; then
                    current_nickname="$nickname_base"
                    nickname_base_used=true
                else
                    current_nickname="${nickname_base}${next_nickname_num}"
                    next_nickname_num=$((next_nickname_num + 1))
                fi
            else
                current_nickname="$current_bot_name"
            fi
        fi
        
        CURRENT_BOT_DIR="${BOTS_ROOT}/${current_bot_name}"
        
        # Check if container exists
        if [ "$(docker ps -a -q -f name=^/${current_bot_name}$)" ]; then
            echo -e "${RED}Skipping '$current_bot_name' (container already exists)${NC}"
            continue
        fi
        
        if [ -d "$CURRENT_BOT_DIR" ]; then
            echo -e "${RED}Skipping '$current_bot_name' (folder already exists)${NC}"
            continue
        fi
        
        echo ""
        echo -e "${YELLOW}Creating bot '$current_bot_name'...${NC}"
        mkdir -p "$CURRENT_BOT_DIR"
    
    # Copy default config
    cp "$CONFIG_SOURCE" "$CURRENT_BOT_DIR/config.json"
    
    # No cookies file is created, copied or mounted. YouTube signs in with a
    # device code, so a cookies.txt would be a stale credential sitting in
    # plaintext in the bot's folder doing nothing.
    COOKIES_MOUNT=""
    CONTAINER_COOKIE_PATH=""
    if [ -f "$CURRENT_BOT_DIR/cookies.txt" ]; then
        # Only reachable if a directory was reused. Remove it rather than leave a
        # credential nothing reads.
        rm -f "$CURRENT_BOT_DIR/cookies.txt"
        log_line "Removed a leftover cookies.txt from $CURRENT_BOT_DIR"
    fi

    # Directories the services write their own credentials into.
    mkdir -p "$CURRENT_BOT_DIR/secrets" "$CURRENT_BOT_DIR/browser"              "$CURRENT_BOT_DIR/youtube_auth" "$CURRENT_BOT_DIR/librespot"
    chown -R 1000:1000 "$CURRENT_BOT_DIR" 2>/dev/null || true
    chmod 700 "$CURRENT_BOT_DIR/secrets" "$CURRENT_BOT_DIR/youtube_auth"               "$CURRENT_BOT_DIR/librespot" 2>/dev/null || true
    
    # Update JSON with jq
    tmp_config=$(mktemp)
    # An empty answer must give an empty list, not a list holding one empty
    # string, which the bot would try to run as a command on every connect.
    if [ -n "${start_command:-}" ]; then
        start_commands_json=$(jq -cn --arg c "$start_command" '[$c]')
    else
        start_commands_json='[]'
    fi
    jq --argjson startcmds "$start_commands_json" \
       --arg host "$server_addr" \
       --argjson tcp "$tcp_port" \
       --argjson udp "$udp_port" \
       --argjson enc "$encrypted" \
       --arg nick "$current_nickname" \
       --arg user "$username" \
       --arg pass "$password" \
       --arg chan "$channel" \
       --arg chan_pass "$channel_password" \
       --argjson del_timer "$delete_timer" \
       '.teamtalk.hostname = $host |
        .teamtalk.tcp_port = $tcp |
        .teamtalk.udp_port = $udp |
        .teamtalk.encrypted = $enc |
        .teamtalk.nickname = $nick |
        .teamtalk.username = $user |
        .teamtalk.password = $pass |
        .teamtalk.channel = $chan |
        .teamtalk.channel_password = $chan_pass |
        .general.delete_uploaded_files_after = $del_timer |
        .general.start_commands = $startcmds' \
       "$CURRENT_BOT_DIR/config.json" > "$tmp_config" && mv "$tmp_config" "$CURRENT_BOT_DIR/config.json"

    # Fix permissions for container user (uid 1000 is standard for non-root in many images)
    echo "Adjusting folder permissions..."
    chown -R 1000:1000 "$CURRENT_BOT_DIR"

    log_line "Creating bot $current_bot_name: host=$server_addr tcp=$tcp_port udp=$udp_port encrypted=$encrypted channel=$channel nickname=$nickname startup=${start_command:-none}"
    # Deliberately absent from that line: the account password and the channel
    # password. A log that records those is a log that leaks them.
    log_say "Creating the container."
    # docker create output went to /dev/null, so a failure said only "Error
    # creating" with no reason. It now goes to the log, and is shown when the
    # command fails.
    if log_run "docker create for $current_bot_name" \
        docker create \
            --name "${current_bot_name}" \
            --network host \
            -e "TTBOT_INSTANCE=${current_bot_name}" \
            -e "YOUTUBE_BRIDGE_URL=${YOUTUBE_BRIDGE_URL}" \
            --label "role=streamerbot" \
            --restart always \
            -v "${CURRENT_BOT_DIR}:/home/streamer/StreamerBot/data" \
            "${BOT_IMAGE}"; then
        log_say "OK. Bot $current_bot_name created."
    else
        log_say "Error. Bot $current_bot_name was not created."
    fi
    done
    
    echo ""
    echo -e "${YELLOW}Starting all bots in parallel...${NC}"
    # Start all newly created bots in parallel
    ensure_shared_youtube_service || return
    docker start $(docker ps -a -q -f "label=role=streamerbot" -f "status=created") 2>/dev/null
    
    echo -e "${GREEN}Creation completed! $total_bots bot(s) created and started.${NC}"
    rm -f /tmp/cookies_pasted.txt
    read -p "Press Enter to return..."
}

# Function: List Bots
list_bots() {
    echo -e "${YELLOW}Existing Bots:${NC}"
    if [ -d "$BOTS_ROOT" ]; then
        ls -1 "$BOTS_ROOT"
    else
        echo "No bots found."
    fi
        echo ""
}

# Function: Delete Bot
delete_bot() {
    # Show menu once
    header
    while true; do
        echo -e "${YELLOW} --- Delete Bot --- ${NC}"
        
        # Array to store bot names
        bots=()
        if [ -d "$BOTS_ROOT" ]; then
            for d in "$BOTS_ROOT"/*; do
                if [ -d "$d" ]; then
                    bots+=("$(basename "$d")")
                fi
            done
        fi
        
        if [ ${#bots[@]} -eq 0 ]; then
            echo "No bots found."
            read -p "Enter to return..."
            return
        fi
        
        echo "Bots available for deletion:"
        for i in "${!bots[@]}"; do
            # Display 1-based index
            echo "$((i+1)). ${bots[$i]}"
        done
        echo "0. Return"
        echo ""
        
        read -p "Enter the NUMBER of the bot to DELETE: " bot_num
        
        # Handle Empty (Enter key) - Just refresh
        if [[ -z "$bot_num" ]]; then
            echo ""
            continue
        fi
        
        # Handle Return
        if [[ "$bot_num" == "0" ]]; then return; fi
        
        # Validate input
        if [[ ! "$bot_num" =~ ^[0-9]+$ ]] || [ "$bot_num" -lt 1 ] || [ "$bot_num" -gt "${#bots[@]}" ]; then
            # Invalid option - just reprint menu
            echo ""
            continue
        fi
        
        # Get bot name by index (adjust for 1-based input)
        idx=$((bot_num-1))
        bot_to_delete="${bots[$idx]}"
        
        CONTAINER_NAME="${bot_to_delete}"
        BOT_DIR="${BOTS_ROOT}/${bot_to_delete}"
        
        echo "Warning. This deletes the bot '$bot_to_delete' completely: its container and its"
        echo "folder, which holds its configuration and every connected account."
        echo "This cannot be undone."
        echo ""
        read -p "Type delete to confirm, or press Enter to cancel: " confirm
        if [ "$confirm" = "delete" ]; then
            echo "1. Removing Container..."
            docker stop -t 1 "$CONTAINER_NAME" >/dev/null 2>&1
            docker rm "$CONTAINER_NAME" >/dev/null 2>&1
            echo "   OK (If existed)."
            
            echo "2. Removing Folder..."
            if [ -d "$BOT_DIR" ]; then
                rm -rf "$BOT_DIR"
                echo "   Folder removed: $BOT_DIR"
            else
                echo "   Folder not found (already removed)."
            fi
            
            echo -e "${GREEN}Cleanup completed for '$bot_to_delete'.${NC}"
            read -p "Press Enter to continue..."
            # Refresh menu after deletion
            header
        else
            echo "Cancelled."
            echo ""
        fi
    done
}

# Function: Delete Multiple Bots at Once
delete_bots_batch() {
    # Show menu once
    header
    while true; do
        echo -e "${YELLOW} --- Bulk Delete Bots --- ${NC}"
        
        # Array to store bot names
        bots=()
        if [ -d "$BOTS_ROOT" ]; then
            for d in "$BOTS_ROOT"/*; do
                if [ -d "$d" ]; then
                    bots+=("$(basename "$d")")
                fi
            done
        fi
        
        if [ ${#bots[@]} -eq 0 ]; then
            echo "No bots found."
            read -p "Enter to return..."
            return
        fi
        
        echo "Bots available for deletion:"
        for i in "${!bots[@]}"; do
            echo "$((i+1)). ${bots[$i]}"
        done
        echo "0. DELETE ALL BOTS"
        echo "r. Return"
        echo ""
        echo -e "${YELLOW}Enter NUMBERS separated by SPACE (ex: 1 2 5):${NC}"
        read -p "> " bot_nums
        
        # Handle Empty (Enter key) - Just refresh
        if [[ -z "$bot_nums" ]]; then
            echo ""
            continue
        fi
        
        # Handle Return
        if [[ "$bot_nums" == "r" || "$bot_nums" == "R" ]]; then return; fi
        
        # Parse and validate numbers
        selected_bots=()
        invalid=false
        
        # Handle Delete All
        if [[ "$bot_nums" == "0" ]]; then
            selected_bots=("${bots[@]}")
        else
            for num in $bot_nums; do
            if [[ ! "$num" =~ ^[0-9]+$ ]] || [ "$num" -lt 1 ] || [ "$num" -gt "${#bots[@]}" ]; then
                echo -e "${RED}Invalid number: $num${NC}"
                invalid=true
                break
            fi
            idx=$((num-1))
            selected_bots+=("${bots[$idx]}")
        done
        fi
        
        if [ "$invalid" = true ]; then
            echo ""
            continue
        fi
        
        # Show summary and confirm
        echo ""
        echo "Warning. This deletes the following bots completely, including every"
        echo "connected account. This cannot be undone."
        for bot in "${selected_bots[@]}"; do
            echo "  $bot"
        done
        echo ""
        read -p "Type delete to confirm, or press Enter to cancel: " confirm

        if [ "$confirm" = "delete" ]; then
            echo ""
            echo -e "${YELLOW}Stopping all selected containers...${NC}"
            # Stop all selected containers at once
            docker stop -t 1 "${selected_bots[@]}" >/dev/null 2>&1
            
            echo -e "${YELLOW}Removing containers...${NC}"
            # Remove all selected containers at once
            docker rm "${selected_bots[@]}" >/dev/null 2>&1
            
            echo -e "${YELLOW}Removing folders...${NC}"
            # Remove directories
            for bot_name in "${selected_bots[@]}"; do
                BOT_DIR="${BOTS_ROOT}/${bot_name}"
                if [ -d "$BOT_DIR" ]; then
                    rm -rf "$BOT_DIR"
                fi
            done
            
            echo ""
            echo -e "${GREEN}Bulk cleanup completed! ${#selected_bots[@]} bot(s) deleted.${NC}"
            read -p "Press Enter to continue..."
            header
        else
            echo "Cancelled."
            echo ""
        fi
    done
}

# Function: Bulk Update Configuration
bulk_update_config() {
    header
    echo -e "${YELLOW} --- Bulk Update Configuration --- ${NC}"
    echo ""
    
    # Get list of bots
    bots=()
    if [ -d "$BOTS_ROOT" ]; then
        for d in "$BOTS_ROOT"/*; do
            if [ -d "$d" ]; then
                bot_name=$(basename "$d")
                bots+=("$bot_name")
            fi
        done
    fi
    
    if [ ${#bots[@]} -eq 0 ]; then
        echo "No bots found."
        read -p "Enter to return..."
        return
    fi
    
    # Read first bot's config as reference
    first_bot="${bots[0]}"
    first_config="$BOTS_ROOT/$first_bot/config.json"
    
    if [ ! -f "$first_config" ]; then
        echo -e "${RED}Error: config.json not found.${NC}"
        read -p "Enter to return..."
        return
    fi
    
    # Extract current values
    current_host=$(jq -r '.teamtalk.hostname // "N/A"' "$first_config")
    current_tcp=$(jq -r '.teamtalk.tcp_port // "N/A"' "$first_config")
    current_udp=$(jq -r '.teamtalk.udp_port // "N/A"' "$first_config")
    current_enc=$(jq -r '.teamtalk.encrypted // false' "$first_config")
    current_user=$(jq -r '.teamtalk.username // "N/A"' "$first_config")
    current_chan=$(jq -r '.teamtalk.channel // "/"' "$first_config")
    current_chan_pass=$(jq -r '.teamtalk.channel_password // ""' "$first_config")
    
    echo -e "${GREEN}Current configuration (reference: $first_bot):${NC}"
    echo "  Server: $current_host"
    echo "  TCP: $current_tcp"
    echo "  UDP: $current_udp"
    echo "  Encryption: $([ "$current_enc" = "true" ] && echo "Yes" || echo "No")"
    echo "  Username: $current_user"
    echo "  Channel: $current_chan"
    echo "  Channel Password: $([ -n "$current_chan_pass" ] && echo "*****" || echo "(None)")"
    echo ""
    echo "Total bots: ${#bots[@]}"
    echo ""
    
    # Show current delete timer from first bot
    current_del_timer=$(jq -r '.general.delete_uploaded_files_after // "N/A"' "$first_config")
    echo "  Delete files after: ${current_del_timer}s"
    echo ""

    # Menu for field selection
    while true; do
        echo "What do you want to change?"
        echo "1. Server (hostname)"
        echo "2. Ports (TCP/UDP)"
        echo "3. Encryption"
        echo "4. Credentials (username/password)"
        echo "5. Channel & Password"
        echo "6. File Deletion Timer (delete_uploaded_files_after)"
        echo "7. Everything"
        echo "0. Cancel"
        echo ""
        read -p "Choose an option: " choice

        if [[ -z "$choice" ]]; then
            echo ""
            continue
        fi

        case $choice in
            0)
                return
                ;;
            1|2|3|4|5|6|7)
                break
                ;;
            *)
                echo ""
                continue
                ;;
        esac
    done
    
    # Collect new values based on choice
    # Initialize with UNSET to distinguish between "keep current" and "clear"
    new_host="UNSET"
    new_tcp="UNSET"
    new_udp="UNSET"
    new_enc="UNSET"
    new_user="UNSET"
    new_pass="UNSET"
    new_chan="UNSET"
    new_chan_pass="UNSET"
    new_del_timer="UNSET"
    
    echo ""
    
    if [[ "$choice" == "1" || "$choice" == "7" ]]; then
        read -p "New server (Enter = keep): " input
        if [ -n "$input" ]; then new_host="$input"; fi
    fi

    if [[ "$choice" == "2" || "$choice" == "7" ]]; then
        read -p "New TCP port (Enter = keep): " input
        if [ -n "$input" ]; then new_tcp="$input"; fi
        read -p "New UDP port (Enter = keep): " input
        if [ -n "$input" ]; then new_udp="$input"; fi
    fi
    
    if [[ "$choice" == "3" || "$choice" == "7" ]]; then
        read -p "Encryption (y/N): " enc_input
        if [[ "$enc_input" =~ ^[yY]$ ]]; then
            new_enc="true"
        elif [[ "$enc_input" =~ ^[nN]$ ]]; then
            new_enc="false"
        fi
    fi

    if [[ "$choice" == "4" || "$choice" == "7" ]]; then
        read -p "New username (Enter = keep, '.' = clear): " input
        if [ "$input" == "." ]; then new_user=""; elif [ -n "$input" ]; then new_user="$input"; fi

        read -p "New password (Enter = keep, '.' = clear): " input
        if [ "$input" == "." ]; then new_pass=""; elif [ -n "$input" ]; then new_pass="$input"; fi
    fi
    
    if [[ "$choice" == "5" || "$choice" == "7" ]]; then
        read -p "New Channel (Enter = keep, '.' = root '/'): " input
        if [ "$input" == "." ]; then new_chan="/"; elif [ -n "$input" ]; then new_chan="$input"; fi

        read -p "New Channel Password (Enter = keep, '.' = clear): " input
        if [ "$input" == "." ]; then new_chan_pass=""; elif [ -n "$input" ]; then new_chan_pass="$input"; fi
    fi

    if [[ "$choice" == "6" || "$choice" == "7" ]]; then
        echo ""
        echo -e "${YELLOW}Delete uploaded files after how many seconds? (0 = never delete)${NC}"
        echo "  Current (from first bot): ${current_del_timer}s"
        read -p "New timer in seconds (Enter = keep): " input
        if [ -n "$input" ]; then
            if [[ "$input" =~ ^[0-9]+$ ]]; then
                new_del_timer="$input"
            else
                echo -e "${RED}Invalid value, keeping current.${NC}"
            fi
        fi
    fi
    
    # Show summary
    echo ""
    echo -e "${YELLOW}Changes summary:${NC}"
    [ "$new_host" != "UNSET" ] && echo "  Server: $new_host"
    [ "$new_tcp" != "UNSET" ] && echo "  TCP: $new_tcp"
    [ "$new_udp" != "UNSET" ] && echo "  UDP: $new_udp"
    [ "$new_enc" != "UNSET" ] && echo "  Encryption: $([ "$new_enc" = "true" ] && echo "Yes" || echo "No")"

    if [ "$new_user" != "UNSET" ]; then
        if [ -z "$new_user" ]; then echo "  Username: (Cleared)"; else echo "  Username: $new_user"; fi
    fi

    if [ "$new_pass" != "UNSET" ]; then
        if [ -z "$new_pass" ]; then echo "  Password: (Cleared)"; else echo "  Password: ********"; fi
    fi

    if [ "$new_chan" != "UNSET" ]; then
        echo "  Channel: $new_chan"
    fi

    if [ "$new_chan_pass" != "UNSET" ]; then
        if [ -z "$new_chan_pass" ]; then echo "  Channel Password: (Cleared)"; else echo "  Channel Password: ********"; fi
    fi

    if [ "$new_del_timer" != "UNSET" ]; then
        echo "  Delete files after: ${new_del_timer}s$([ "$new_del_timer" == "0" ] && echo " (never)" || true)"
    fi

    echo ""
    echo "Will be applied to ${#bots[@]} bot(s) total"
    echo ""

    # --- Target selection ---
    echo -e "${YELLOW}Apply changes to:${NC}"
    echo "1. ALL bots (${#bots[@]})"
    echo "2. A specific bot"
    echo "3. Select specific bots"
    echo ""
    read -p "Choose target [Default: 1]: " target_choice
    target_choice=${target_choice:-1}

    target_bots=()

    case "$target_choice" in
        1)
            target_bots=("${bots[@]}")
            ;;
        2)
            echo ""
            echo "Available bots:"
            for i in "${!bots[@]}"; do
                echo "  $((i+1)). ${bots[$i]}"
            done
            echo ""
            read -p "Enter the NUMBER of the bot: " bot_num
            if [[ ! "$bot_num" =~ ^[0-9]+$ ]] || [ "$bot_num" -lt 1 ] || [ "$bot_num" -gt "${#bots[@]}" ]; then
                echo -e "${RED}Invalid number. Cancelled.${NC}"
                read -p "Enter to return..."
                return
            fi
            target_bots=("${bots[$((bot_num-1))]}")
            ;;
        3)
            echo ""
            echo "Available bots:"
            for i in "${!bots[@]}"; do
                echo "  $((i+1)). ${bots[$i]}"
            done
            echo ""
            echo -e "${YELLOW}Enter numbers separated by SPACE (ex: 1 3 5):${NC}"
            read -p "> " bot_nums
            invalid=false
            for num in $bot_nums; do
                if [[ ! "$num" =~ ^[0-9]+$ ]] || [ "$num" -lt 1 ] || [ "$num" -gt "${#bots[@]}" ]; then
                    echo -e "${RED}Invalid number: $num${NC}"
                    invalid=true
                    break
                fi
                target_bots+=("${bots[$((num-1))]}")
            done
            if [ "$invalid" = true ] || [ ${#target_bots[@]} -eq 0 ]; then
                echo -e "${RED}No valid bots selected. Cancelled.${NC}"
                read -p "Enter to return..."
                return
            fi
            ;;
        *)
            echo -e "${RED}Invalid option. Cancelled.${NC}"
            read -p "Enter to return..."
            return
            ;;
    esac

    # Final confirmation
    echo ""
    echo -e "${YELLOW}Will apply to ${#target_bots[@]} bot(s):${NC}"
    for b in "${target_bots[@]}"; do echo "  - $b"; done
    echo ""
    read -p "Confirm changes? (y/N): " confirm

    if [[ ! "$confirm" =~ ^[yY]$ ]]; then
        echo "Cancelled."
        read -p "Enter to return..."
        return
    fi

    # Update selected bot configs
    echo ""
    echo -e "${YELLOW}Updating configurations...${NC}"

    for bot_name in "${target_bots[@]}"; do
        config_file="$BOTS_ROOT/$bot_name/config.json"

        if [ ! -f "$config_file" ]; then
            echo "  ⚠ Skipping $bot_name (config.json not found)"
            continue
        fi

        tmp_config=$(mktemp)

        # Build jq command dynamically
        jq_cmd="."

        if [ "$new_host" != "UNSET" ]; then
            jq_cmd="$jq_cmd | .teamtalk.hostname = \"$new_host\""
        fi

        if [ "$new_tcp" != "UNSET" ]; then
            jq_cmd="$jq_cmd | .teamtalk.tcp_port = $new_tcp"
        fi

        if [ "$new_udp" != "UNSET" ]; then
            jq_cmd="$jq_cmd | .teamtalk.udp_port = $new_udp"
        fi

        if [ "$new_enc" != "UNSET" ]; then
            jq_cmd="$jq_cmd | .teamtalk.encrypted = $new_enc"
        fi

        if [ "$new_user" != "UNSET" ]; then
            jq_cmd="$jq_cmd | .teamtalk.username = \"$new_user\""
        fi

        if [ "$new_pass" != "UNSET" ]; then
            jq_cmd="$jq_cmd | .teamtalk.password = \"$new_pass\""
        fi

        if [ "$new_chan" != "UNSET" ]; then
            jq_cmd="$jq_cmd | .teamtalk.channel = \"$new_chan\""
        fi

        if [ "$new_chan_pass" != "UNSET" ]; then
            jq_cmd="$jq_cmd | .teamtalk.channel_password = \"$new_chan_pass\""
        fi

        if [ "$new_del_timer" != "UNSET" ]; then
            jq_cmd="$jq_cmd | .general.delete_uploaded_files_after = $new_del_timer"
        fi

        jq "$jq_cmd" "$config_file" > "$tmp_config" && mv "$tmp_config" "$config_file"

        # Fix permissions for container user
        chown 1000:1000 "$config_file"

        echo "  ✓ $bot_name updated"
    done

    # Restart only affected bots
    echo ""
    echo -e "${YELLOW}Restarting affected bots...${NC}"
    for bot_name in "${target_bots[@]}"; do
        docker stop -t 1 "$bot_name" > /dev/null 2>&1
        docker start "$bot_name" > /dev/null 2>&1
        echo "  ↺ $bot_name restarted"
    done

    echo ""
    echo -e "${GREEN}Configuration updated successfully for ${#target_bots[@]} bot(s)!${NC}"
    read -p "Press Enter to continue..."
}

# Function: Duplicate Bot
duplicate_bot() {
    # Show menu once
    header
    while true; do
        echo -e "${YELLOW} --- Duplicate Bot --- ${NC}"
        
        # Array to store bot info
        bots=()
        bot_servers=()
        
        if [ -d "$BOTS_ROOT" ]; then
            for d in "$BOTS_ROOT"/*; do
                if [ -d "$d" ]; then
                    bot_name=$(basename "$d")
                    bots+=("$bot_name")
                    
                    # Extract server address from config.json
                    config_file="$d/config.json"
                    if [ -f "$config_file" ]; then
                        server=$(jq -r '.teamtalk.hostname // "N/A"' "$config_file" 2>/dev/null)
                        bot_servers+=("$server")
                    else
                        bot_servers+=("N/A")
                    fi
                fi
            done
        fi
        
        if [ ${#bots[@]} -eq 0 ]; then
            echo "No bots found."
            read -p "Enter to return..."
            return
        fi
        
        echo "Bots available to duplicate:"
        for i in "${!bots[@]}"; do
            echo "$((i+1)). ${bots[$i]} → ${bot_servers[$i]}"
        done
        echo "0. Return"
        echo ""
        read -p "Enter the NUMBER of the bot to DUPLICATE: " bot_num
        
        # Handle Empty (Enter key) - Just refresh
        if [[ -z "$bot_num" ]]; then
            echo ""
            continue
        fi
        
        # Handle Return
        if [[ "$bot_num" == "0" ]]; then return; fi
        
        # Validate input
        if [[ ! "$bot_num" =~ ^[0-9]+$ ]] || [ "$bot_num" -lt 1 ] || [ "$bot_num" -gt "${#bots[@]}" ]; then
            echo ""
            continue
        fi
        
        # Get source bot
        idx=$((bot_num-1))
        source_bot="${bots[$idx]}"
        SOURCE_BOT_DIR="${BOTS_ROOT}/${source_bot}"
        
        echo ""
        echo -e "${GREEN}Duplicating bot: $source_bot${NC}"
        echo ""
        
        # Ask for new base name and validate it doesn't already exist
        while true; do
            read -p "Enter NEW BASE NAME for the bot(s) (Enter = default 'bot'): " new_base_name
            if [[ -z "$new_base_name" ]]; then
                new_base_name="bot"
            fi
            
            if [ "$(docker ps -a -q -f name=^/${new_base_name}$)" ] || [ -d "${BOTS_ROOT}/${new_base_name}" ]; then
                echo -e "${RED}Error: A bot with the exact name '${new_base_name}' already exists.${NC}"
                echo "Please type another name."
                echo ""
                continue
            fi
            break
        done
        
        # Ask for nickname base
        read -p "BASE name for NICKNAMES (Enter = same as container '$new_base_name'): " nickname_base
        if [[ -z "$nickname_base" ]]; then
            nickname_base="$new_base_name"
        fi
        
        # Find highest existing number for both container names and nicknames
        highest_num=0
        base_name_exists=false
        highest_nickname_num=0
        nickname_base_exists=false
        
        
        if [ -d "$BOTS_ROOT" ]; then
            # Get source bot's server info for comparison
            source_hostname=$(jq -r '.teamtalk.hostname // ""' "$SOURCE_BOT_DIR/config.json")
            source_tcp_port=$(jq -r '.teamtalk.tcp_port // 0' "$SOURCE_BOT_DIR/config.json")
            
            for d in "$BOTS_ROOT"/*; do
                if [ -d "$d" ]; then
                    name=$(basename "$d")
                    
                    # Strictly check containers
                    if [[ "$name" == "$new_base_name" ]]; then
                        base_name_exists=true
                    elif [[ "$name" =~ ^${new_base_name}([0-9]+)$ ]]; then
                        n="${BASH_REMATCH[1]}"
                        [ "$n" -gt "$highest_num" ] && highest_num=$n
                    fi
                    
                    # Strictly check nicknames - BUT ONLY for bots on the SAME SERVER
                    config_file="$d/config.json"
                    if [ -f "$config_file" ]; then
                        # Get this bot's server info
                        existing_hostname=$(jq -r '.teamtalk.hostname // ""' "$config_file")
                        existing_tcp_port=$(jq -r '.teamtalk.tcp_port // 0' "$config_file")
                        
                        # Only check nicknames if it's the SAME server
                        if [[ "$existing_hostname" == "$source_hostname" ]] && [[ "$existing_tcp_port" == "$source_tcp_port" ]]; then
                            nick=$(jq -r '.teamtalk.nickname // ""' "$config_file")
                            if [[ "$nick" == "$nickname_base" ]]; then
                                nickname_base_exists=true
                            elif [[ "$nick" =~ ^${nickname_base}([0-9]+)$ ]]; then
                                n="${BASH_REMATCH[1]}"
                                [ "$n" -gt "$highest_nickname_num" ] && highest_nickname_num=$n
                            fi
                        fi
                    fi
                fi
            done
        fi
        
        # Ask for quantity
        read -p "How many ADDITIONAL bots to create (0 = only the base)?: " additional_bots
        if [[ ! "$additional_bots" =~ ^[0-9]+$ ]]; then
            echo -e "${RED}Invalid quantity.${NC}"
            echo ""
            continue
        fi
        
        total_bots=$((additional_bots + 1))
        echo ""
        echo -e "${YELLOW}Creating $total_bots duplicated bot(s)...${NC}"
        
        # Simple sequential counter for naming
        if [ "$base_name_exists" == "true" ]; then
            next_container_num=$((highest_num + 1))
        else
            next_container_num=1
        fi
        
        if [ "$nickname_base_exists" == "true" ]; then
            next_nickname_num=$((highest_nickname_num + 1))
        else
            next_nickname_num=1
        fi
        
        # Track if we've used the base name yet
        container_base_used=$base_name_exists
        nickname_base_used=$nickname_base_exists
        
        # Loop to create duplicated bots
        for i in $(seq 1 $total_bots); do
            # Determine container name - always use base naming scheme
            if [ "$container_base_used" == "false" ]; then
                current_bot_name="$new_base_name"
                container_base_used=true
            else
                current_bot_name="${new_base_name}${next_container_num}"
                next_container_num=$((next_container_num + 1))
            fi

            # Determine nickname - same logic
            if [ "$nickname_base_used" == "false" ]; then
                current_nickname="$nickname_base"
                nickname_base_used=true
            else
                current_nickname="${nickname_base}${next_nickname_num}"
                next_nickname_num=$((next_nickname_num + 1))
            fi
            
            CURRENT_BOT_DIR="${BOTS_ROOT}/${current_bot_name}"
            
            # Check if container exists
            if [ "$(docker ps -a -q -f name=^/${current_bot_name}$)" ]; then
                echo -e "${RED}Skipping '$current_bot_name' (container already exists)${NC}"
                continue
            fi
            
            if [ -d "$CURRENT_BOT_DIR" ]; then
                echo -e "${RED}Skipping '$current_bot_name' (folder already exists)${NC}"
                continue
            fi
            
            echo ""
            echo -e "${YELLOW}Creating bot '$current_bot_name' (Nickname: $current_nickname)...${NC}"
            mkdir -p "$CURRENT_BOT_DIR"
            
            # Copy config from source bot
            cp "$SOURCE_BOT_DIR/config.json" "$CURRENT_BOT_DIR/config.json"
            
            # Update nickname
            tmp_config=$(mktemp)
            jq --arg nick "$current_nickname" '.teamtalk.nickname = $nick' "$CURRENT_BOT_DIR/config.json" > "$tmp_config" && mv "$tmp_config" "$CURRENT_BOT_DIR/config.json"
            
            # No cookies file is carried across. Duplicating a bot duplicates
            # its configuration, not its sign-ins: the copy connects its own
            # accounts, which is also what keeps two bots from sharing one.
            rm -f "$CURRENT_BOT_DIR/cookies.txt"
            
            # Fix permissions
            chown -R 1000:1000 "$CURRENT_BOT_DIR"
            
            # Create container (without starting)
            docker create \
                --name "${current_bot_name}" \
                --network host \
                -e "TTBOT_INSTANCE=${current_bot_name}" \
                -e "YOUTUBE_BRIDGE_URL=${YOUTUBE_BRIDGE_URL}" \
                --label "role=streamerbot" \
                --restart always \
                -v "${CURRENT_BOT_DIR}:/home/streamer/StreamerBot/data" \
                "${BOT_IMAGE}" > /dev/null 2>&1
            
            if [ $? -eq 0 ]; then
                echo "  ✓ Bot '$current_bot_name' created"
            else
                echo "  ✗ Error creating '$current_bot_name'"
            fi
        done
        
        echo ""
        echo -e "${YELLOW}Starting all bots in parallel...${NC}"
        # Start all newly created bots in parallel
        ensure_shared_youtube_service || return
        docker start $(docker ps -a -q -f "label=role=streamerbot" -f "status=created") 2>/dev/null
        
        echo -e "${GREEN}Duplication completed! $total_bots bot(s) created and started.${NC}"
        read -p "Press Enter to continue..."
        header
    done
}


# Function: Restart All with Timer
restart_with_timer() {
    header
    echo -e "${YELLOW} --- Restart with Timer (Exit and Return) --- ${NC}"
    
    echo "This will STOP all bots, wait for the defined time, and START them again."
    read -p "Enter wait time in SECONDS (ex: 5): " wait_time
    
    if [[ ! "$wait_time" =~ ^[0-9]+$ ]]; then
        echo -e "${RED}Invalid time.${NC}"
        read -p "Enter to return..."
        return
    fi
    
    echo -e "${YELLOW}Stopping all bots...${NC}"
    docker stop -t 1 $(docker ps -a -q -f "label=role=streamerbot")
    
    echo -e "${YELLOW}Waiting ${wait_time} seconds...${NC}"
    # A carriage-return countdown rewrites one line every second, and a screen
    # reader reads the whole line again on each rewrite, talking over the user
    # for the entire wait. The two surrounding messages say the same thing.
    sleep "$wait_time"
    
    echo -e "${YELLOW}Starting all bots...${NC}"
    docker start $(docker ps -a -q -f "label=role=streamerbot")
    
    echo -e "${GREEN}Process completed.${NC}"
    read -p "Enter to return..."
}

# ---------------------------------------------------------------------------
# Uninstall.
#
# "Uninstall everything" hid three very different outcomes behind one label, so
# it now asks which. The least destructive is first and is what Enter selects,
# because the common reason for coming here is reclaiming disk space or forcing
# a clean rebuild, not throwing away accounts.
#
# Level 1 deliberately leaves bots/ untouched. That directory holds every bot's
# YouTube tokens, encrypted service passwords, browser profiles and
# configuration, so preserving it means streamerbot.sh afterwards rebuilds and
# the bots come back exactly as they were.
# ---------------------------------------------------------------------------
remove_bot_containers_and_images() {
    echo ""
    echo "Step 1 of 3. Stopping and removing bot containers."
    local ids
    ids=$(docker ps -a -q -f "label=role=${STREAMERBOT_LABEL:-streamerbot}" 2>/dev/null)
    if [ -n "$ids" ]; then
        docker rm -f $ids >/dev/null 2>&1
        echo "OK. Bot containers removed."
    else
        echo "OK. There were no bot containers."
    fi

    echo "Step 2 of 3. Removing the shared YouTube service."
    if docker rm -f "$YOUTUBE_SERVICE_NAME" >/dev/null 2>&1; then
        echo "OK. Shared YouTube service removed."
    else
        echo "OK. There was no shared YouTube service."
    fi

    echo "Step 3 of 3. Removing the StreamerBot image."
    if docker rmi -f "$BOT_IMAGE" >/dev/null 2>&1; then
        echo "OK. Image removed."
    else
        echo "OK. There was no image to remove."
    fi
}

uninstall_all() {
    header
    echo "Uninstall"
    echo ""
    echo "There are three levels. Choose how much to remove."
    echo ""
    echo "1. Just the containers and the image."
    echo "   Removes every bot container, the shared YouTube service, and the"
    echo "   StreamerBot image. Your bots folder is left completely alone, so every"
    echo "   connected account and all configuration survive and the bots come back"
    echo "   when you rebuild. This is the safe option."
    echo ""
    echo "2. The containers, the image, and all bot data."
    echo "   Everything in option 1, plus the bots folder, the auto-update service"
    echo "   and temporary files. This deletes every connected account and cannot be"
    echo "   undone. Take a backup first if you may want any of it back."
    echo ""
    echo "3. All of that, and Docker itself."
    echo "   Everything in option 2, plus a full Docker prune and stopping the Docker"
    echo "   engine. Warning. This affects containers that have nothing to do with"
    echo "   StreamerBot, so it can break other software on this machine."
    echo ""
    echo "b. Back, without removing anything."
    echo ""
    read -p "Choose 1, 2, 3, or b [Enter = 1]: " level
    level="${level:-1}"

    case "$level" in
        1)
            echo "You selected 1, just the containers and the image."
            echo ""
            echo "Your bots folder and every connected account will be kept."
            read -p "Type remove to confirm, or press Enter to cancel: " confirm
            if [ "$confirm" != "remove" ]; then
                echo "Cancelled. Nothing was removed."
                read -p "Press Enter to continue..."
                return
            fi
            remove_bot_containers_and_images
            echo ""
            echo "OK. Done. Your bots folder was not touched."
            echo "Run this script again to rebuild and start the bots."
            read -p "Press Enter to continue..."
            ;;
        2|3)
            # Both of these are uninstall.sh's territory: it already knows how to
            # remove bot data and how to take Docker down, and duplicating that
            # here would mean two things to keep correct.
            if [ ! -f "${SCRIPT_DIR}/uninstall.sh" ]; then
                echo "Error. uninstall.sh was not found, so this cannot continue."
                read -p "Press Enter to continue..."
                return
            fi
            if [ "$level" = "2" ]; then
                echo "You selected 2, the containers, the image, and all bot data."
            else
                echo "You selected 3, all of that and Docker itself."
            fi
            echo ""
            exec bash "${SCRIPT_DIR}/uninstall.sh"
            ;;
        b|B)
            echo "You selected b, back. Nothing was removed."
            ;;
        *)
            echo "That was not one of the choices. Enter 1, 2, 3, or b."
            read -p "Press Enter to continue..."
            ;;
    esac
}



# Function: Clean Unused Docker Resources
clean_docker_unused() {
    header
    echo -e "${YELLOW} --- Clean Unused Docker Resources --- ${NC}"
    echo "This will remove all stopped containers, unused images, volumes and cache."
    echo -e "${GREEN}Running containers will NOT be affected.${NC}"
    echo ""
    read -p "Proceed with cleanup? (y/N): " confirm
    
    if [[ ! "$confirm" =~ ^[yY]$ ]]; then
        return
    fi
    
    echo -e "${YELLOW}Cleaning Docker system...${NC}"
    docker system prune -af --volumes
    
    echo -e "${YELLOW}Cleaning Buildx cache...${NC}"
    docker buildx prune -af
    docker builder prune -af
    
    echo -e "${YELLOW}Cleaning System Logs (journald)...${NC}"
    journalctl --vacuum-time=1d
    
    echo ""
    echo -e "${GREEN}Done! Space reclaimed.${NC}"
    read -p "Press Enter to continue..."
}

# Function: Backup Bots
backup_bots() {
    header
    echo -e "${YELLOW} --- Backup Bots Config & Cache --- ${NC}"
    
    if [ ! -d "$BOTS_ROOT" ] || [ -z "$(ls -A "$BOTS_ROOT" 2>/dev/null)" ]; then
        echo -e "${RED}Error: No bots found to backup.${NC}"
        read -p "Press Enter to continue..."
        return
    fi
    
    BACKUP_DIR="${SCRIPT_DIR}/backups"
    mkdir -p "$BACKUP_DIR"
    
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    BACKUP_FILE="${BACKUP_DIR}/backup_bots_${TIMESTAMP}.tar.gz"
    
    echo "Backing up the bots folder to:"
    echo "  $BACKUP_FILE"
    echo ""
    echo "Warning. This archive contains the sign-in details for every connected"
    echo "account, including the key that decrypts the stored passwords. Treat it"
    echo "like the passwords themselves: do not paste it anywhere, and keep it"
    echo "somewhere only you can read."
    echo ""

    # Browser caches are excluded. A Chrome profile's caches run to hundreds of
    # megabytes per service per bot, they regenerate on first use, and including
    # them makes a backup slow enough that people stop taking them. Cookies and
    # Local Storage are NOT excluded: those are the signed-in session, which is
    # the thing worth keeping.
    #
    # Logs and the media cache go too, for the same reason: large, and worthless
    # once restored.
    echo "Step 1 of 2. Building the archive, without browser caches or logs."
    tar -czf "$BACKUP_FILE" -C "$SCRIPT_DIR"         --exclude='bots/*/browser/*/Cache'         --exclude='bots/*/browser/*/Code Cache'         --exclude='bots/*/browser/*/GPUCache'         --exclude='bots/*/browser/*/DawnGraphiteCache'         --exclude='bots/*/browser/*/DawnWebGPUCache'         --exclude='bots/*/browser/*/Service Worker/CacheStorage'         --exclude='bots/*/browser/*/component_crx_cache'         --exclude='bots/*/browser/*/Crashpad'         --exclude='bots/*/*.log'         --exclude='bots/*/StreamerBotCache.dat'         bots
    tar_status=$?

    if [ "$tar_status" -eq 0 ]; then
        echo "Step 2 of 2. Checking the archive."
        size=$(du -h "$BACKUP_FILE" 2>/dev/null | cut -f1)
        echo "OK. Backup complete. Size: ${size:-unknown}."
    else
        echo "Error. The backup failed and the archive may be incomplete."
        rm -f "$BACKUP_FILE"
    fi
    read -p "Press Enter to continue..."
}

# Function: Restore Bots
# ---------------------------------------------------------------------------
# Migrating a restored backup.
#
# A backup from the old TTMediaBot has none of the sections this version needs
# and carries names this version no longer uses.
#
# Such a config does still start: every new section has a default, so pydantic
# fills them in silently. That is exactly the problem. The settings then exist
# only in memory, so nobody can see or edit them, the cache and log keep their
# old names and the bot's history is orphaned under a file it no longer writes,
# and the credential directories do not exist with the ownership the container
# needs. The bot appears to work and then cannot connect an account.
#
# So the migration writes the new sections to disk where they can be read and
# changed, rather than leaving them as invisible defaults.
#
# So every restored bot is checked and brought up to date BEFORE any container is
# created. Three rules govern all of it:
#
#   The bot's identity is sacred. Its directory name, its TeamTalk nickname,
#   username, password, status text, channel and channel password are what make
#   it the same bot to the people on that server. A restore that silently renamed
#   a bot or reset its nickname would be worse than one that failed outright.
#
#   Never overwrite a value the user already had. Missing keys are filled from
#   defaults and existing ones are left exactly as they were, which is why the
#   merge is "defaults * existing" and not the other way round.
#
#   Never delete anything. Files this version stopped using stay where they are.
#   A restore is not the moment to throw away something someone may still want.
# ---------------------------------------------------------------------------

# Fields that identify the bot to a TeamTalk server. Read before the migration
# and checked afterwards, because getting these wrong is the one failure mode
# nobody would forgive.
BOT_IDENTITY_FIELDS='.teamtalk.nickname, .teamtalk.username, .teamtalk.password, .teamtalk.status, .teamtalk.channel, .teamtalk.channel_password, .teamtalk.hostname, .teamtalk.tcp_port, .general.language'

bot_identity_fingerprint() {
    jq -c "[${BOT_IDENTITY_FIELDS}]" "$1" 2>/dev/null
}

# Sections this version needs. Anything already in the bot's config wins.
streamerbot_config_defaults() {
    cat <<'DEFAULTSJSON'
  {
    "services": {
      "sp": { "enabled": true, "device_name": "StreamerBot", "api_port": 3678, "client_id": "" },
      "nf": { "enabled": true, "profile": "" },
      "dp": { "enabled": true, "profile": "" },
      "am": { "enabled": true, "profile": "" },
      "az": { "enabled": true, "profile": "" }
    },
    "auth_portal": {
      "enabled": true,
      "host": "127.0.0.1",
      "port": 4419,
      "public_url": "",
      "token_ttl": 72000
    },
    "audio_description": { "default": "ask" },
    "sound_devices": { "output_device_name": "", "input_device_name": "" }
  }
DEFAULTSJSON
}

# True when this bot directory predates the current version.
bot_dir_is_legacy() {
    local dir="$1"
    [ -f "$dir/TTMediaBotCache.dat" ] && return 0
    [ -f "$dir/TTMediaBot.log" ] && return 0
    if [ -f "$dir/config.json" ]; then
        grep -q "TTMediaBot" "$dir/config.json" 2>/dev/null && return 0
        # No auth_portal section means it predates this version whatever it is named.
        jq -e 'has("auth_portal")' "$dir/config.json" >/dev/null 2>&1 || return 0
    fi
    return 1
}

migrate_one_bot() {
    local dir="$1" name before after
    name=$(basename "$dir")

    # Directories the new services need, created empty. A restore signs nothing
    # in; the bot creates what it needs inside them.
    local sub
    for sub in secrets browser youtube_auth librespot; do
        [ -d "$dir/$sub" ] || mkdir -p "$dir/$sub"
    done

    # Renamed files, moved only when the new name is absent, so re-running is safe.
    if [ -f "$dir/TTMediaBotCache.dat" ] && [ ! -f "$dir/StreamerBotCache.dat" ]; then
        mv "$dir/TTMediaBotCache.dat" "$dir/StreamerBotCache.dat"
        echo "  Renamed the cache file, keeping its contents."
    fi
    if [ -f "$dir/TTMediaBot.log" ] && [ ! -f "$dir/StreamerBot.log" ]; then
        mv "$dir/TTMediaBot.log" "$dir/StreamerBot.log"
        echo "  Renamed the log file."
    fi

    if [ -f "$dir/config.json" ]; then
        before=$(bot_identity_fingerprint "$dir/config.json")
        if [ -z "$before" ]; then
            echo "  Warning. config.json could not be read as JSON, so it was left untouched."
            echo "  This bot will not start until that file is valid."
            return 1
        fi

        local tmp defaults
        tmp=$(mktemp)
        defaults=$(streamerbot_config_defaults)

        # Deep merge with the bot's own values winning, then the few renames a
        # merge cannot express, because the old value is still valid JSON and
        # would simply be kept. Note that nothing here touches .teamtalk.
        if ! jq --argjson d "$defaults" '
                ($d * .)
                | .general.cache_file_name =
                    (if ((.general.cache_file_name // "") | test("TTMediaBot"))
                     then "StreamerBotCache.dat"
                     else (.general.cache_file_name // "StreamerBotCache.dat") end)
                | .logger.file_name =
                    (if ((.logger.file_name // "") | test("TTMediaBot"))
                     then "StreamerBot.log"
                     else (.logger.file_name // "StreamerBot.log") end)
                | .config_version = (if ((.config_version // 0) < 2) then 2 else .config_version end)
              ' "$dir/config.json" > "$tmp" 2>/dev/null; then
            rm -f "$tmp"
            echo "  Warning. The configuration could not be updated and was left untouched."
            return 1
        fi

        # Refuse the change if the bot's identity moved. Better to leave a bot on
        # the old config and say so than to bring it back under a different name.
        after=$(bot_identity_fingerprint "$tmp")
        if [ "$before" != "$after" ]; then
            rm -f "$tmp"
            echo "  Error. The update would have changed this bot's name or server details,"
            echo "  so it was abandoned and the original configuration kept."
            return 1
        fi

        if cmp -s "$tmp" "$dir/config.json"; then
            rm -f "$tmp"
        else
            # The untouched original stays beside the new one. If this migration
            # got something wrong, that file is the way back.
            cp "$dir/config.json" "$dir/config.json.pre-migration"
            mv "$tmp" "$dir/config.json"
            echo "  Added the new configuration sections. Nickname and server details unchanged."
            echo "  The original is kept as config.json.pre-migration."
        fi

        # The old cookies.txt is removed. Nothing reads it since the switch to
        # device-code sign-in, and leaving it behind means a stale YouTube
        # session sitting in plaintext in a directory that gets tarred into
        # backups. Deleting it is the safer of the two options, not the riskier
        # one, and the bot signs in again with a code.
        if [ -f "$dir/cookies.txt" ]; then
            rm -f "$dir/cookies.txt"
            log_line "Removed the obsolete cookies.txt from $name"
            echo "  Removed the old cookies.txt. This version signs in with a code"
            echo "  instead. Send li yt to the bot once it is running."
        fi
    fi

    # The container runs as uid 1000 and must be able to write the credential
    # directories it was just given.
    chown -R 1000:1000 "$dir" 2>/dev/null || true
    chmod 700 "$dir/secrets" "$dir/youtube_auth" "$dir/librespot" 2>/dev/null || true
    return 0
}

migrate_restored_bots() {
    [ -d "$BOTS_ROOT" ] || return 0

    if ! command -v jq >/dev/null 2>&1; then
        echo "Warning. jq is not installed, so restored configurations cannot be checked."
        echo "Install jq and run Restore again, or the bots may not start."
        return 1
    fi

    local dir name legacy_count=0 total=0
    for dir in "$BOTS_ROOT"/*; do
        [ -d "$dir" ] || continue
        total=$((total + 1))
        bot_dir_is_legacy "$dir" && legacy_count=$((legacy_count + 1))
    done

    [ "$total" -gt 0 ] || return 0

    echo "Step 1 of 2. Checking the restored bots."
    if [ "$legacy_count" -eq 0 ]; then
        echo "OK. All $total restored bots already have the current configuration."
    else
        echo "This backup came from an older version. $legacy_count of $total bots need updating."
        echo "Their configurations are brought up to date before any container is created."
        echo "Every bot keeps its name, its nickname and its server details, and nothing"
        echo "is deleted."
    fi
    echo ""

    local failed=0
    for dir in "$BOTS_ROOT"/*; do
        [ -d "$dir" ] || continue
        name=$(basename "$dir")
        echo "Bot $name:"
        if migrate_one_bot "$dir"; then
            echo "  OK."
        else
            failed=$((failed + 1))
        fi
    done

    echo ""
    if [ "$failed" -gt 0 ]; then
        echo "Warning. $failed bots could not be updated and may not start."
        echo "Their original configurations were left untouched."
        return 1
    fi
    echo "OK. Every bot is ready."
    return 0
}

restore_bots() {
    header
    echo -e "${YELLOW} --- Restore Bots Config & Cache --- ${NC}"
    
    BACKUP_DIR="${SCRIPT_DIR}/backups"
    if [ ! -d "$BACKUP_DIR" ] || [ -z "$(ls -A "$BACKUP_DIR"/*.tar.gz 2>/dev/null)" ]; then
        echo -e "${RED}No backup files (.tar.gz) found in 'backups/' directory.${NC}"
        read -p "Press Enter to continue..."
        return
    fi
    
    # List available backups
    echo "Available Backups:"
    backups=()
    for f in "$BACKUP_DIR"/*.tar.gz; do
        if [ -f "$f" ]; then
            backups+=("$(basename "$f")")
        fi
    done
    
    for i in "${!backups[@]}"; do
        echo "$((i+1)). ${backups[$i]}"
    done
    echo "0. Return"
    echo ""
    
    read -p "Choose a backup number to restore: " choice
    if [[ -z "$choice" || "$choice" == "0" ]]; then
        return
    fi
    
    if [[ ! "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#backups[@]}" ]; then
        echo -e "${RED}Invalid choice.${NC}"
        read -p "Press Enter to continue..."
        return
    fi
    
    selected_backup="${backups[$((choice-1))]}"
    selected_path="${BACKUP_DIR}/${selected_backup}"
    
    echo "Warning. This replaces the current bots folder and every bot's configuration"
    echo "with the contents of that backup. Anything connected since the backup was"
    echo "taken will be lost."
    echo ""
    read -p "Type restore to confirm, or press Enter to cancel: " confirm
    if [ "$confirm" != "restore" ]; then
        return
    fi
    
    echo ""
    echo -e "${YELLOW}Stopping all running bots...${NC}"
    # Stop containers
    RUNNING_BOTS=$(docker ps -q -f "label=role=streamerbot")
    if [ -n "$RUNNING_BOTS" ]; then
        docker stop -t 1 $RUNNING_BOTS >/dev/null 2>&1
    fi
    
    # Remove containers
    ALL_BOTS=$(docker ps -a -q -f "label=role=streamerbot")
    if [ -n "$ALL_BOTS" ]; then
        echo -e "${YELLOW}Removing existing bot containers...${NC}"
        docker rm $ALL_BOTS >/dev/null 2>&1
    fi
    
    echo -e "${YELLOW}Restoring 'bots/' folder...${NC}"
    # Delete current bots folder
    rm -rf "$BOTS_ROOT"
    
    # Extract
    tar -xzf "$selected_path" -C "$SCRIPT_DIR"
    
    if [ $? -eq 0 ]; then
        echo "OK. Extraction complete."
        echo ""

        # Before any container exists. A bot started against an old config either
        # fails validation or comes up missing half its services, and either looks
        # like the restore having gone wrong.
        migrate_restored_bots
        migration_status=$?
        if [ "$migration_status" -ne 0 ]; then
            echo ""
            echo "Warning. Some bots were not updated. Continuing, but check them before use."
        fi
        echo ""
        echo "Step 2 of 2. Recreating the containers."
        echo "Refreshing the shared YouTube service mount."
        create_shared_youtube_service || {
            echo -e "${RED}Could not recreate the shared YouTube service.${NC}"
            read -p "Press Enter to continue..."
            return
        }
        echo -e "${YELLOW}Recreating bot containers from restored configs...${NC}"
        
        # We need to recreate containers
        recreate_bot_containers
        
        # Start them
        echo -e "${YELLOW}Starting restored bots...${NC}"
        docker start $(docker ps -a -q -f "label=role=streamerbot") >/dev/null 2>&1
        start_shared_youtube_service || {
            echo -e "${RED}Bots were restored, but the shared YouTube service failed to start.${NC}"
            read -p "Press Enter to continue..."
            return
        }
        
        echo -e "${GREEN}Restore completed and bots started!${NC}"
    else
        echo -e "${RED}Error during extraction!${NC}"
    fi
    read -p "Press Enter to continue..."
}

# Function: Backup/Restore Menu
backup_restore_menu() {
    while true; do
        header
        echo -e "${YELLOW} --- Backup / Restore Bots --- ${NC}"
        echo "1. Backup current bots configuration & cache"
        echo "2. Restore bots from a backup"
        echo "3. Return to Manage Bots"
        echo ""
        read -p "Choose an option: " opt_br
        
        case $opt_br in
            1)
                backup_bots
                ;;
            2)
                restore_bots
                ;;
            3|*)
                return
                ;;
        esac
    done
}

# Function: Clear Bot Logs
clear_bot_logs() {
    header
    echo -e "${YELLOW} --- Clear All Bot Logs --- ${NC}"
    echo "This will delete all *.log files in all bot folders."
    echo ""
    read -p "Are you sure you want to clear all log files? (y/N): " confirm
    if [[ ! "$confirm" =~ ^[yY]$ ]]; then
        return
    fi
    
    echo ""
    if [ -d "$BOTS_ROOT" ]; then
        # Find and delete .log files
        find "$BOTS_ROOT" -name "*.log" -type f -delete
        echo -e "${GREEN}All log files deleted!${NC}"
    else
        echo -e "${YELLOW}No bots folder found. Nothing to clear.${NC}"
    fi
    read -p "Press Enter to continue..."
}

# Function: Clear Bot Cache Files
clear_bot_caches() {
    header
    echo -e "${YELLOW} --- Clear All Bot Cache Files --- ${NC}"
    echo "This will delete all *.cache and *.dat files in all bot folders."
    echo ""
    read -p "Are you sure you want to clear all bot cache files? (y/N): " confirm
    if [[ ! "$confirm" =~ ^[yY]$ ]]; then
        return
    fi

    echo ""
    if [ -d "$BOTS_ROOT" ]; then
        find "$BOTS_ROOT" -type f \( -name "*.cache" -o -name "*.dat" \) -delete
        echo -e "${GREEN}All bot cache files deleted!${NC}"
    else
        echo -e "${YELLOW}No bots folder found. Nothing to clear.${NC}"
    fi
    read -p "Press Enter to continue..."
}

# Function: Clear YouTube Bridge Cache
clear_youtube_bridge_cache() {
    header
    echo -e "${YELLOW} --- Clear YouTube Bridge Cache --- ${NC}"
    echo "This will clear in-memory and on-disk search & stream caches."
    echo ""
    read -p "Are you sure you want to clear YouTube Bridge cache? (y/N): " confirm
    if [[ ! "$confirm" =~ ^[yY]$ ]]; then
        return
    fi

    echo ""
    echo "Clearing YouTube Bridge persistent cache..."
    rm -f "${SCRIPT_DIR}/youtube_bridge/bridge_cache.json" 2>/dev/null || true
    
    if docker inspect "$YOUTUBE_SERVICE_NAME" >/dev/null 2>&1; then
        docker exec "$YOUTUBE_SERVICE_NAME" rm -f /home/streamer/StreamerBot/youtube_bridge/bridge_cache.json 2>/dev/null || true
        echo "Restarting YouTube Bridge service to flush RAM..."
        docker restart "$YOUTUBE_SERVICE_NAME" >/dev/null 2>&1 || true
    fi

    echo -e "${GREEN}YouTube Bridge cache cleared successfully!${NC}"
    read -p "Press Enter to continue..."
}

# Function: Manage Bots
manage_bots() {
    # Show menu once
    header
    while true; do
        echo -e "${YELLOW} --- Manage Bots --- ${NC}"
        echo "1. Start All (With label role=streamerbot)"
        echo "2. Restart All (With label role=streamerbot)"
        echo "3. Stop All (With label role=streamerbot)"
        echo "4. Delete Bot"
        echo "5. Bulk Delete Bots"
        echo "6. Duplicate Bot"
        echo "7. Restart with Timer (Stop, Wait, Start)"
        echo "8. Bulk Update Configuration"
        echo "9. Backup and Restore Bots"
        echo "10. Clear All Bot Logs"
        echo "11. Clear All Bot Cache Files"
        echo "12. Clear YouTube Bridge Cache"
        echo "13. Return to Main Menu"
        echo ""
        read -p "Choose an option: " opt_manage
        
        # Handle empty input - just reprint menu
        if [ -z "$opt_manage" ]; then
            echo ""
            continue
        fi
        
        case $opt_manage in
            1)
                echo "Starting all bots..."
                docker start $(docker ps -a -q -f "label=role=streamerbot")
                read -p "Completed. Enter to continue..."
                header
                ;;
            2)
                echo "Restarting all bots..."
                echo "  Stopping..."
                docker stop -t 1 $(docker ps -a -q -f "label=role=streamerbot")
                echo "  Starting..."
                docker start $(docker ps -a -q -f "label=role=streamerbot")
                read -p "Completed. Enter to continue..."
                header
                ;;
            3)
                echo "Stopping all bots..."
                docker stop -t 1 $(docker ps -a -q -f "label=role=streamerbot")
                read -p "Completed. Enter to continue..."
                header
                ;;
            4)
                delete_bot
                header
                ;;
            5)
                delete_bots_batch
                header
                ;;
            6)
                duplicate_bot
                header
                ;;
            7)
                restart_with_timer
                header
                ;;
            8)
                bulk_update_config
                header
                ;;
            9)
                backup_restore_menu
                header
                ;;
            10)
                clear_bot_logs
                header
                ;;
            11)
                clear_bot_caches
                header
                ;;
            12)
                clear_youtube_bridge_cache
                header
                ;;
            13)
                return
                ;;
            *)
                # Invalid option - just reprint menu
                echo ""
                ;;
        esac
    done
}


# ---------------------------------------------------------------------------
# Startup: make sure we know which GitHub repository to update from, then do a
# cheap passive update check.
#
# This deliberately does NOT run an update. Earlier versions ran the whole of
# update.sh on every launch, which meant opening the manager could rebuild the
# image and restart every bot without being asked. Now the user is only told
# that an update exists; menu item "Check for updates" installs it.
# ---------------------------------------------------------------------------
ensure_project_env() {
    local env_file="$SCRIPT_DIR/project.env"
    [ -f "$env_file" ] || return 0

    # shellcheck disable=SC1090
    . "$env_file"

    [ -n "${STREAMERBOT_REPO_OWNER:-}" ] && return 0

    echo ""
    echo "Setup. StreamerBot does not know which GitHub account to get updates from."
    echo "Enter the GitHub username that owns your StreamerBot repository."
    echo "Press Enter on its own to skip. Updates stay disabled until this is set."
    echo ""
    local owner=""
    read -r -p "GitHub username: " owner
    owner="$(echo "$owner" | tr -d '[:space:]')"

    if [ -z "$owner" ]; then
        echo "Warning. No username entered, so automatic updates are disabled for now."
        echo "You can set STREAMERBOT_REPO_OWNER in project.env at any time."
        return 0
    fi

    if sed -i "s|^STREAMERBOT_REPO_OWNER=.*|STREAMERBOT_REPO_OWNER=${owner}|" "$env_file"; then
        STREAMERBOT_REPO_OWNER="$owner"
        echo "OK. Updates will come from https://github.com/${owner}/${STREAMERBOT_REPO_NAME:-StreamerBot}"
    else
        echo "Error. Could not write to project.env. Set STREAMERBOT_REPO_OWNER by hand."
    fi
}

check_for_updates_passive() {
    [ -f "$SCRIPT_DIR/update.sh" ] || return 0
    [ -n "${STREAMERBOT_REPO_OWNER:-}" ] || return 0

    local notice
    notice="$(bash "$SCRIPT_DIR/update.sh" --check-only 2>/dev/null)"
    if [ -n "$notice" ]; then
        echo ""
        echo "$notice"
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# Non-interactive flags.
#
# Experienced screen reader users skip menus: a flag is one line of typing and
# gives its answer immediately, where a menu is several rounds of listening to
# options to reach the same place. These also make the manager scriptable.
#
# Handled after project.env is loaded, so the image and service names are known,
# and before the menu is drawn, so nothing interactive happens.
# ---------------------------------------------------------------------------
print_cli_help() {
    echo "StreamerBot manager"
    echo ""
    echo "Run with no arguments for the menu, or use one of these:"
    echo ""
    echo "  --status         List every bot and whether it is running."
    echo "  --services       Show the shared YouTube service and the image."
    echo "  --start-all      Start every bot."
    echo "  --stop-all       Stop every bot."
    echo "  --restart-all    Restart every bot."
    echo "  --check-updates  Say whether an update is available, without installing it."
    echo "  --logs NAME      Show the last 50 log lines for one bot."
    echo "  --help           This text."
}

cli_bot_names() {
    [ -d "$BOTS_ROOT" ] || return 0
    local dir
    for dir in "$BOTS_ROOT"/*; do
        [ -d "$dir" ] && basename "$dir"
    done
}

cli_status() {
    local found=0 name state
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        found=1
        state=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo "no container")
        echo "$name: $state"
    done < <(cli_bot_names)
    [ "$found" -eq 1 ] || echo "There are no bots yet."
}

cli_services() {
    local state
    state=$(docker inspect -f '{{.State.Status}}' "$YOUTUBE_SERVICE_NAME" 2>/dev/null || echo "not created")
    echo "Shared YouTube service: $state"
    if curl -fsS "$YOUTUBE_BRIDGE_URL/health" >/dev/null 2>&1; then
        echo "YouTube bridge: responding"
    else
        echo "YouTube bridge: not responding"
    fi
    if docker image inspect "$BOT_IMAGE" >/dev/null 2>&1; then
        echo "Image $BOT_IMAGE: present"
    else
        echo "Image $BOT_IMAGE: missing. Run this script with no arguments to build it."
    fi
}

cli_for_each_bot() {
    local action="$1" name count=0
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        if docker "$action" "$name" >/dev/null 2>&1; then
            echo "OK. $action $name."
        else
            echo "Error. Could not $action $name."
        fi
        count=$((count + 1))
    done < <(cli_bot_names)
    [ "$count" -gt 0 ] || echo "There are no bots yet."
}

case "${1:-}" in
    --help|-h)
        print_cli_help
        exit 0
        ;;
    --status)
        cli_status
        exit 0
        ;;
    --services)
        cli_services
        exit 0
        ;;
    --start-all)
        cli_for_each_bot start
        exit 0
        ;;
    --stop-all)
        cli_for_each_bot stop
        exit 0
        ;;
    --restart-all)
        cli_for_each_bot restart
        exit 0
        ;;
    --check-updates)
        if [ -f "$SCRIPT_DIR/update.sh" ]; then
            notice="$(bash "$SCRIPT_DIR/update.sh" --check-only 2>/dev/null)"
            if [ -n "$notice" ]; then
                echo "$notice"
            else
                echo "OK. StreamerBot is up to date."
            fi
        else
            echo "Error. update.sh was not found."
        fi
        exit 0
        ;;
    --logs)
        if [ -z "${2:-}" ]; then
            echo "Error. Give a bot name, for example: streamerbot.sh --logs mybot"
            exit 1
        fi
        docker logs --tail 50 "$2" 2>&1 || {
            echo "Error. No container named $2."
            exit 1
        }
        exit 0
        ;;
    "")
        ;;
    *)
        echo "Error. Unknown option: $1"
        echo ""
        print_cli_help
        exit 1
        ;;
esac

ensure_project_env
check_for_updates_passive

build_image
if docker run --rm --entrypoint test "$BOT_IMAGE" -f /home/streamer/StreamerBot/youtube_services.sh; then
    ensure_shared_youtube_service || exit 1
else
    echo -e "${YELLOW}Shared YouTube service requires an image rebuild (option 3).${NC}"
fi

# Main Menu
mkdir -p "$BOTS_ROOT"

# Show menu once
header
while true; do
    echo "1. Create Bot"
    echo "2. Manage Bots"
    echo "3. Rebuild Image / Update Code"
    echo "4. Uninstall Everything (Total Cleanup)"
    echo "5. Check for Updates"
    echo "6. Enable/Disable Auto-Updates"
    echo "7. Clean Docker Cache (Unused)"
    echo "8. Manage Shared YouTube Servers"
    echo "9. Exit"
    echo ""
    read -p "Choose an option: " option
    
    # Handle empty input - just reprint menu
    if [ -z "$option" ]; then
        echo ""
        continue
    fi
    
    case $option in
        1)
            create_bot
            header
            ;;
        2)
            manage_bots
            header
            ;;
        3)
            force_rebuild_image
            header
            ;;
        4)
            uninstall_all
            ;;
        5)
            if [ -f "update.sh" ]; then
                bash update.sh
            else
                echo -e "${RED}update.sh not found.${NC}"
                read -p "Press Enter to continue..."
            fi
            header
            ;;
        6)
            if [ -f "$SCRIPT_DIR/masc.sh" ]; then
                bash "$SCRIPT_DIR/masc.sh"
            else
                echo -e "${RED}masc.sh not found.${NC}"
                read -p "Press Enter to continue..."
            fi
            header
            ;;
        7)
            clean_docker_unused
            header
            ;;
        8)
            if [ -f "$SCRIPT_DIR/youtube_server_manager.sh" ]; then
                bash "$SCRIPT_DIR/youtube_server_manager.sh"
            else
                echo -e "${RED}youtube_server_manager.sh not found.${NC}"
                read -p "Press Enter to continue..."
            fi
            header
            ;;
        9)
            echo "Exiting..."
            exit 0
            ;;
        *)
            # Invalid option - just reprint menu
            echo ""
            ;;
    esac
done
