#!/bin/bash

# masc.sh - StreamerBot Auto-Update Controller
# This script manages the masking and activation of the auto-updater service.

# Auto-elevate to root via sudo if needed
if [ "$EUID" -ne 0 ]; then
    echo "Not running as root. Re-launching with sudo..."
    exec sudo bash "$0" "$@"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SERVICE_NAME="streamerbot-updater.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
# shellcheck disable=SC1091
[ -f "$SCRIPT_DIR/project.env" ] && . "$SCRIPT_DIR/project.env"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/update_channel.sh"


# Template to ensure we can always restore the service
SERVICE_TEMPLATE="[Unit]
Description=StreamerBot Auto-Updater Watcher
After=network.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=/bin/bash $SCRIPT_DIR/auto_updater.sh
Restart=always
User=root

[Install]
WantedBy=multi-user.target"

header() {
    clear
    echo -e "${GREEN}=========================================${NC}"
    echo -e "${GREEN}      StreamerBot Auto-Update Manager     ${NC}"
    echo -e "${GREEN}=========================================${NC}"
    echo ""
}

get_status() {
    # Use LANG=C to ensure English output for status checks
    local enabled_status=$(LANG=C systemctl is-enabled "$SERVICE_NAME" 2>/dev/null)
    local active_status=$(LANG=C systemctl is-active "$SERVICE_NAME" 2>/dev/null)

    # Check if masked
    if [[ "$enabled_status" == "masked" ]]; then
        echo -e "  Masked:  ${RED}Yes (Disabled)${NC}"
    else
        echo -e "  Masked:  ${GREEN}No (Allowed)${NC}"
    fi

    # Check if enabled
    if [ "$enabled_status" == "enabled" ]; then
        echo -e "  Enabled: ${GREEN}Yes${NC}"
    else
        echo -e "  Enabled: ${RED}No${NC}"
    fi

    # Check if active
    if [ "$active_status" == "active" ]; then
        echo -e "  Service: ${GREEN}Active (Running)${NC}"
    elif [ "$active_status" == "activating" ]; then
        echo -e "  Service: ${YELLOW}Activating...${NC}"
    else
        echo -e "  Service: ${RED}Inactive (Stopped)${NC}"
    fi
}

enable_auto_update() {
    echo -e "${YELLOW}Enabling Auto-Updates...${NC}"
    
    # 1. Unmask
    systemctl unmask "$SERVICE_NAME"
    
    # 2. Recreate service file from template
    echo "Creating service file..."
    echo "$SERVICE_TEMPLATE" > "$SERVICE_PATH"
    chmod 644 "$SERVICE_PATH"
    
    # 3. Reload, enable, and start
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl start "$SERVICE_NAME"
    
    echo -e "${GREEN}Auto-Updates enabled!${NC}"
    sleep 2
}

disable_auto_update() {
    echo -e "${YELLOW}Disabling Auto-Updates...${NC}"
    
    # 1. Aggressive process cleanup first to prevent systemctl hang
    echo "Cleaning up processes..."
    killall -9 auto_updater.sh sleep 2>/dev/null
    
    # 2. Stop and disable (non-blocking)
    systemctl stop --no-block "$SERVICE_NAME"
    systemctl disable "$SERVICE_NAME"
    
    # 3. Remove the service file or symlink
    rm -f "$SERVICE_PATH"
    
    # 4. Mask and reload
    systemctl mask "$SERVICE_NAME"
    systemctl daemon-reload
    systemctl reset-failed "$SERVICE_NAME"
    
    echo -e "${GREEN}Auto-Updates disabled and masked!${NC}"
    sleep 2
}

# Stable or latest. Choosing stable on a server that runs newer code than the
# last release offers to wait for the next one, which is the default because
# older code can refuse a configuration a newer version has migrated.
choose_update_channel() {
    local current choice wanted branch target
    current="$(update_channel)"
    echo ""
    echo "This server follows the $current channel: $(channel_description "$current")."
    echo ""
    echo "1. Stable: $(channel_description stable)"
    echo "2. Latest: $(channel_description latest)"
    echo "3. Return"
    echo ""
    read -p "Choose an option: " choice
    case "$choice" in
        1) wanted=stable ;;
        2) wanted=latest ;;
        *) return 0 ;;
    esac
    if ! set_update_channel "$wanted"; then
        echo -e "${RED}Error. Could not write $UPDATE_CHANNEL_FILE.${NC}"
        sleep 2
        return 1
    fi
    echo -e "${GREEN}OK. This server now follows the $wanted channel.${NC}"

    branch="$(update_branch)"
    if git fetch -q origin "$branch" 2>/dev/null \
            && target="$(git rev-parse "origin/$branch" 2>/dev/null)" \
            && [ "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" != "$branch" ] \
            && channel_would_downgrade "$target"; then
        echo ""
        echo "This server runs newer code than the $wanted channel, which is at $(describe_commit "$target")."
        echo "1. Stay on this code until a release passes it (recommended)"
        echo "2. Move back to $(describe_commit "$target") now. Bots whose configuration a newer version changed may refuse to start."
        read -p "Choose an option: " choice
        if [ "$choice" = "2" ]; then
            STREAMERBOT_ALLOW_DOWNGRADE=true bash "$SCRIPT_DIR/update.sh"
        fi
        return 0
    fi
    read -p "Update from the $wanted channel now? [y/N]: " choice
    if [[ "$choice" =~ ^[yY]$ ]]; then
        bash "$SCRIPT_DIR/update.sh"
    fi
}

while true; do
    header
    echo "Current Status:"
    get_status
    echo ""
    echo "1. Enable Auto-Updates"
    echo "2. Disable Auto-Updates"
    echo "3. Return to Main Menu"
    echo "4. Choose Update Channel (now $(update_channel): $(channel_description "$(update_channel)"))"
    echo ""
    read -p "Choose an option: " choice
    
    case $choice in
        1)
            enable_auto_update
            ;;
        2)
            disable_auto_update
            ;;
        3)
            exit 0
            ;;
        4)
            choose_update_channel
            ;;
        *)
            echo -e "${RED}Invalid option.${NC}"
            sleep 1
            ;;
    esac
done
