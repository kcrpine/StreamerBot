#!/bin/bash

# ================================================================= #
# StreamerBot Uninstaller Submenu
# ================================================================= #

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOTS_ROOT="${SCRIPT_DIR}/bots"
BOT_IMAGE="streamerbot"

# Auto-elevate to root via sudo if needed
if [ "$EUID" -ne 0 ]; then
    echo "Not running as root. Re-launching with sudo..."
    exec sudo bash "$0" "$@"
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

header() {
    clear
    echo -e "${RED}        StreamerBot Uninstaller           ${NC}"
    echo ""
}

return_to_main() {
    if [ -f "${SCRIPT_DIR}/streamerbot.sh" ]; then
        exec "${SCRIPT_DIR}/streamerbot.sh"
    else
        exit 0
    fi
}

# -----------------------------------------------------------------
# OPTION 1: Standard & Safe Uninstall (RECOMMENDED)
# Removes ONLY StreamerBot resources (containers, image, data, updater).
# Preserves Docker Engine, system packages, and other Docker projects.
# -----------------------------------------------------------------
uninstall_bot_only() {
    header
    echo -e "${YELLOW}--- OPTION 1: Standard StreamerBot Uninstall (Safe & Recommended) ---${NC}"
    echo ""
    echo "EXPLICIT DETAILS OF WHAT WILL BE REMOVED:"
    echo "  1. All StreamerBot Docker containers and shared YouTube service"
    echo "  2. The StreamerBot Docker image ('${BOT_IMAGE}')"
    echo "  3. All bot configuration, log, and cookies directories ('${BOTS_ROOT}')"
    echo "  4. The StreamerBot auto-updater systemd service (streamerbot-updater.service)"
    echo "  5. StreamerBot temporary lock and cache files (/tmp/streamerbot_*)"
    echo ""
    echo -e "${GREEN}SYSTEM PRESERVATION GUARANTEE:${NC}"
    echo "  - Docker Engine WILL NOT be removed or stopped."
    echo "  - System packages (git, curl, jq, etc.) WILL NOT be touched."
    echo "  - Containers, images, or volumes from OTHER projects WILL NOT be affected."
    echo ""

    read -p "Are you sure you want to proceed with Standard Uninstall? (y/N): " confirm
    if [[ ! "$confirm" =~ ^[yY]$ ]]; then
        echo -e "${YELLOW}Action cancelled by user.${NC}"
        sleep 1.5
        return
    fi

    echo ""
    echo -e "${YELLOW}[1/5] Stopping and removing StreamerBot containers...${NC}"
    if command -v docker &>/dev/null; then
        docker rm -f streamerbot-youtube 2>/dev/null
        docker stop -t 1 $(docker ps -a -q -f "label=role=streamerbot") 2>/dev/null
        docker rm $(docker ps -a -q -f "label=role=streamerbot") 2>/dev/null
    fi

    echo -e "${YELLOW}[2/5] Removing StreamerBot Docker image...${NC}"
    if command -v docker &>/dev/null; then
        docker rmi "$BOT_IMAGE" 2>/dev/null
    fi

    echo -e "${YELLOW}[3/5] Removing bot data directories (configs/cookies/logs)...${NC}"
    if [ -d "$BOTS_ROOT" ]; then
        rm -rf "$BOTS_ROOT"
        echo "   Directory '$BOTS_ROOT' removed."
    fi

    echo -e "${YELLOW}[4/5] Disabling auto-updater service...${NC}"
    if command -v systemctl &> /dev/null; then
        systemctl stop streamerbot-updater.service 2>/dev/null
        systemctl disable streamerbot-updater.service 2>/dev/null
        rm -f /etc/systemd/system/streamerbot-updater.service
        systemctl daemon-reload 2>/dev/null
    else
        rm -f /etc/systemd/system/streamerbot-updater.service
    fi

    echo -e "${YELLOW}[5/5] Cleaning temporary files...${NC}"
    rm -f /tmp/streamerbot_update.lock
    rm -f /tmp/streamerbot_last_running.txt
    rm -f /tmp/cookies_pasted.txt
    rm -f /tmp/streamerbot_destroy_*.sh

    echo ""
    echo -e "${GREEN}  STREAMERBOT UNINSTALLED SUCCESSFULLY.   ${NC}"
    echo ""
    read -p "Press Enter to return to main menu..."
    return_to_main
}

# -----------------------------------------------------------------
# OPTION 2: Complete System Purge (DESTRUCTIVE)
# Purges Docker Engine, system dependencies, iptables, system files.
# -----------------------------------------------------------------
uninstall_full_system() {
    header
    echo -e "${RED}--- OPTION 2: Complete System Purge (DESTRUCTIVE) ---${NC}"
    echo -e "${RED}WARNING: THIS ACTION REMOVES DOCKER ENGINE AND SYSTEM PACKAGES!${NC}"
    echo ""
    echo -e "${RED}DISCLAIMER OF LIABILITY:${NC}"
    echo -e "${RED}THE DEVELOPER/AUTHOR ASSUMES ABSOLUTELY NO RESPONSIBILITY OR LIABILITY FOR ANY DAMAGE, DATA LOSS, SYSTEM INSTABILITY, OR DOWN-TIME CAUSED BY EXECUTING THIS DESTRUCTIVE PURGE OPTION.${NC}"
    echo ""
    echo -e "${RED}DO NOT RUN THIS OPTION ON A PRODUCTION SERVER OR A SHARED SERVER CONTAINING OTHER CRITICAL SERVICES OR CONTAINERS!${NC}"
    echo ""
    echo "EXPLICIT DETAILS OF WHAT WILL BE PURGED:"
    echo "  1. ALL StreamerBot containers, images, and data directories"
    echo "  2. Docker Engine daemon and ALL Docker volumes/networks"
    echo "     (WARNING: This deletes ALL OTHER Docker containers running on this server!)"
    echo "  3. System utility packages: git, curl, jq, gnupg, lsb-release"
    echo "  4. Docker APT repositories and GPG keys"
    echo "  5. Docker system directories (/var/lib/docker, /etc/docker, etc.)"
    echo "  6. System iptables firewall rules for Docker and docker0 network interface"
    echo ""

    read -p "To accept the disclaimer and confirm TOTAL SYSTEM DESTRUCTION, type 'yes': " confirm
    if [ "$confirm" != "yes" ]; then
        echo -e "${YELLOW}Action cancelled by user.${NC}"
        sleep 1.5
        return
    fi

    echo ""
    echo -e "${YELLOW}[1/7] Stopping and removing ALL containers and Docker resources...${NC}"
    if command -v docker &>/dev/null; then
        docker rm -f streamerbot-youtube 2>/dev/null
        docker stop -t 1 $(docker ps -a -q -f "label=role=streamerbot") 2>/dev/null
        docker rm $(docker ps -a -q -f "label=role=streamerbot") 2>/dev/null
        docker system prune -a -f --volumes 2>/dev/null
    fi

    echo -e "${YELLOW}[2/7] Stopping Docker system services...${NC}"
    if command -v systemctl &> /dev/null; then
        systemctl stop docker 2>/dev/null
        systemctl stop docker.socket 2>/dev/null
        systemctl disable docker 2>/dev/null
        systemctl disable docker.socket 2>/dev/null
    fi

    echo -e "${YELLOW}[3/7] Removing bot data directories...${NC}"
    if [ -d "$BOTS_ROOT" ]; then
        rm -rf "$BOTS_ROOT"
    fi

    echo -e "${YELLOW}[4/7] Disabling auto-updater service...${NC}"
    if command -v systemctl &> /dev/null; then
        systemctl stop streamerbot-updater.service 2>/dev/null
        systemctl disable streamerbot-updater.service 2>/dev/null
        rm -f /etc/systemd/system/streamerbot-updater.service
        systemctl daemon-reload 2>/dev/null
    else
        rm -f /etc/systemd/system/streamerbot-updater.service
    fi

    echo -e "${YELLOW}[5/7] Cleaning temporary files...${NC}"
    rm -f /tmp/streamerbot_update.lock
    rm -f /tmp/streamerbot_last_running.txt
    rm -f /tmp/cookies_pasted.txt
    rm -f /tmp/streamerbot_destroy_*.sh

    echo -e "${YELLOW}[6/7] Uninstalling Docker Engine & system packages...${NC}"
    if command -v apt-get &> /dev/null; then
        apt-get purge -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-compose 2>/dev/null
        apt-get purge -y jq curl gnupg lsb-release git 2>/dev/null
    elif command -v dnf &> /dev/null; then
        dnf remove -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-compose jq curl gnupg2 git 2>/dev/null
    elif command -v yum &> /dev/null; then
        yum remove -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-compose jq curl gnupg2 git 2>/dev/null
    elif command -v pacman &> /dev/null; then
        pacman -Rns --noconfirm docker docker-compose jq curl gnupg git 2>/dev/null
    elif command -v zypper &> /dev/null; then
        zypper remove -y docker docker-compose jq curl gpg2 git 2>/dev/null
    elif command -v apk &> /dev/null; then
        apk del docker docker-compose jq curl gnupg git 2>/dev/null
    fi

    echo "   Removing residual Docker system directories..."
    rm -rf /var/lib/docker /var/lib/containerd /etc/docker /etc/apparmor.d/docker /var/run/docker.sock /var/run/docker /run/docker /root/.docker /home/*/.docker /var/log/docker /var/log/containerd /usr/local/bin/docker-compose

    if command -v apt-get &> /dev/null; then
        rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.gpg
        apt-get update -q 2>/dev/null
    fi

    groupdel docker 2>/dev/null || true

    echo -e "${YELLOW}[7/7] Flushing Docker iptables rules & interfaces...${NC}"
    iptables -t filter -F DOCKER 2>/dev/null || true
    iptables -t filter -F DOCKER-ISOLATION-STAGE-1 2>/dev/null || true
    iptables -t filter -F DOCKER-ISOLATION-STAGE-2 2>/dev/null || true
    iptables -t filter -F DOCKER-USER 2>/dev/null || true
    iptables -t nat -F DOCKER 2>/dev/null || true
    iptables -t nat -F POSTROUTING 2>/dev/null || true
    iptables -t filter -X DOCKER 2>/dev/null || true
    iptables -t filter -X DOCKER-ISOLATION-STAGE-1 2>/dev/null || true
    iptables -t filter -X DOCKER-ISOLATION-STAGE-2 2>/dev/null || true
    iptables -t filter -X DOCKER-USER 2>/dev/null || true
    iptables -t nat -X DOCKER 2>/dev/null || true

    ip link set docker0 down 2>/dev/null || true
    ip link delete docker0 2>/dev/null || true

    echo ""
    echo -e "${GREEN}     FULL SYSTEM PURGE COMPLETED.        ${NC}"
    echo ""
    read -p "Press Enter to return to main menu..."
    return_to_main
}

# Main Menu Loop
while true; do
    header
    echo -e "${YELLOW}Please select an uninstallation mode:${NC}"
    echo ""
    echo -e "  ${GREEN}1. Standard Uninstall (Recommended - Removes ONLY StreamerBot)${NC}"
    echo -e "  ${RED}2. Full System Purge (DESTRUCTIVE - Removes Docker Engine & System Packages)${NC}"
    echo "  3. Return to Main Menu"
    echo "  0. Exit"
    echo ""
    read -p "Choose an option [1-3, 0] (Default 1): " opt
    opt=${opt:-1}

    case "$opt" in
        1)
            uninstall_bot_only
            ;;
        2)
            uninstall_full_system
            ;;
        3)
            return_to_main
            ;;
        0)
            echo -e "${GREEN}Exiting uninstaller.${NC}"
            exit 0
            ;;
        *)
            echo -e "${RED}Invalid option. Please try again.${NC}"
            sleep 1.5
            ;;
    esac
done
