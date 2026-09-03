#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="streamerbot-youtube"
HEALTH_URL="http://127.0.0.1:4417/health"

if [ "$EUID" -ne 0 ]; then
    exec sudo bash "$0" "$@"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

header() {
    clear
    echo -e "${GREEN}      Shared YouTube Servers Manager     ${NC}"
    echo ""
}

require_service() {
    if docker inspect "$SERVICE_NAME" >/dev/null 2>&1; then
        return 0
    fi
    echo -e "${RED}Shared YouTube service not found.${NC}"
    echo "Use option 3 in streamerbot.sh to build and create it."
    return 1
}

wait_until_healthy() {
    echo -n "Waiting for shared YouTube servers"
    for _ in $(seq 1 60); do
        if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
            echo -e " [ ${GREEN}OK${NC} ]"
            return 0
        fi
        echo -n "."
        sleep 0.5
    done
    echo -e " [ ${RED}FAILED${NC} ]"
    docker logs --tail 30 "$SERVICE_NAME" 2>&1
    return 1
}

start_servers() {
    require_service || return
    docker start "$SERVICE_NAME" >/dev/null
    wait_until_healthy
}

stop_servers() {
    require_service || return
    docker stop -t 10 "$SERVICE_NAME" >/dev/null
    echo -e "${GREEN}Shared YouTube servers stopped.${NC}"
}

restart_servers() {
    require_service || return
    docker restart -t 10 "$SERVICE_NAME" >/dev/null
    wait_until_healthy
}

while true; do
    header
    printf '  [1] Start Servers\n'
    printf '  [2] Stop Servers\n'
    printf '  [3] Restart Servers\n'
    printf '  [4] Return\n'
    echo ""
    read -p "Choose an option: " option

    case "$option" in
        1) start_servers ;;
        2) stop_servers ;;
        3) restart_servers ;;
        4) exit 0 ;;
        *) continue ;;
    esac

    echo ""
    read -p "Press Enter to continue..."
done
