#!/bin/bash
# YouTube egress: choose how YouTube traffic leaves this host, and change the
# exit address when YouTube blocks the current one.
#
# Sourced by streamerbot.sh, which supplies SCRIPT_DIR, BOTS_ROOT, BOT_IMAGE,
# YOUTUBE_SERVICE_NAME, YOUTUBE_BRIDGE_URL and the helpers used in
# egress_apply. Only defines functions; nothing runs on source.
#
# Why this exists: YouTube refuses stream requests from most datacenter
# addresses ("Sign in to confirm you're not a bot"), signed in or not. Signing
# in does not fix an address. Sending the traffic out through an address
# YouTube trusts does.
#
# Files, all untracked and all holding secrets or per-host state:
#   youtube_proxy.env   YOUTUBE_EGRESS_MODE=direct|warp|vpn|url and YOUTUBE_PROXY_URL
#   youtube_vpn.env     gluetun settings including VPN credentials (0600)
#   youtube_egress_ips.log  exit addresses already used, newest last

EGRESS_NAME="${STREAMERBOT_EGRESS_SERVICE:-streamerbot-egress}"
EGRESS_IMAGE="qmcgaw/gluetun:v3"
# Cloudflare WARP as a proxy. Its one port answers both SOCKS5 and plain HTTP
# proxy requests (measured, not assumed), so it needs no adapter: Node's env
# proxy and the relay's requests only speak HTTP proxies.
WARP_IMAGE="caomingjun/warp:latest"
WARP_INTERNAL_PORT=1080
EGRESS_PROXY_PORT=8888
EGRESS_ENV_FILE="$SCRIPT_DIR/youtube_proxy.env"
EGRESS_VPN_FILE="$SCRIPT_DIR/youtube_vpn.env"
EGRESS_IP_LOG="$SCRIPT_DIR/youtube_egress_ips.log"
EGRESS_ROTATE_TRIES=5
# WARP gave the same exit address after a brand-new registration (measured), so
# more tries would only wait longer for the same answer.
WARP_ROTATE_TRIES=2
# Videos the probe tries. One video is not a test: from this very host an old,
# ordinary video resolved while two recent ones were refused, so a single-video
# probe reports "fine" on an address that is failing users. The last two are
# ones kuhao's log shows being refused. A majority must play.
EGRESS_PROBE_VIDEOS="dQw4w9WgXcQ 48Lrud3Bxpc ZHnelB96JGY"

egress_mode() {
    local mode=""
    [ -f "$EGRESS_ENV_FILE" ] && mode="$(sed -n 's/^YOUTUBE_EGRESS_MODE=//p' "$EGRESS_ENV_FILE" | head -n1)"
    echo "${mode:-direct}"
}

egress_write_env() { # mode proxy_url
    ( umask 077
      printf 'YOUTUBE_EGRESS_MODE=%s\nYOUTUBE_PROXY_URL=%s\n' "$1" "$2" > "$EGRESS_ENV_FILE" )
    YOUTUBE_PROXY_URL="$2"
}

# The proxy has to be reachable from the bridge container (on Docker's default
# bridge) and from bots (on the host network). Loopback is neither, and 0.0.0.0
# would make an open proxy on the server's public address. The bridge gateway
# address is both reachable from each and not exposed outside the machine.
egress_gateway_ip() {
    docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null
}

egress_public_ip() { # through the current proxy; empty if it does not answer
    [ -n "${YOUTUBE_PROXY_URL:-}" ] || return 0
    curl -fsS --max-time 20 --proxy "$YOUTUBE_PROXY_URL" https://api.ipify.org 2>/dev/null
}

egress_first_bot() {
    local dir
    for dir in "$BOTS_ROOT"/*; do
        [ -d "$dir" ] && { basename "$dir"; return 0; }
    done
    return 1
}

# True when a fresh resolve gives a stream that actually delivers bytes.
#
# Three things a weaker test would get wrong: the bridge caches resolves for an
# hour, so the entry is invalidated first or a cached success passes a blocked
# address; resolving is not playing, and googlevideo.com can still refuse the
# fetch, so a few bytes are requested; and that fetch leaves through the same
# proxy the bots use, because the URL is signed for the address that resolved it.
# Uses an existing bot's name, since the bridge keeps per-bot state under
# bots/<id>/ and a made-up name would leave a stray directory there.
egress_probe_one() { # video_id
    local bot body url code proxy_args=()
    bot="$(egress_first_bot)" || { echo "There are no bots to test with."; return 2; }
    body="{\"bot_id\":\"$bot\",\"video_id\":\"$1\"}"
    curl -s -o /dev/null --max-time 20 -X POST "$YOUTUBE_BRIDGE_URL/invalidate" \
        -H 'content-type: application/json' -d "$body"
    url="$(curl -s --max-time 90 -X POST "$YOUTUBE_BRIDGE_URL/resolve" \
        -H 'content-type: application/json' -d "$body" | jq -r '.url // empty' 2>/dev/null)"
    [ -n "$url" ] || return 1
    [ -z "${YOUTUBE_PROXY_URL:-}" ] || proxy_args=(--proxy "$YOUTUBE_PROXY_URL")
    code="$(curl -sL -o /dev/null -w '%{http_code}' --max-time 30 "${proxy_args[@]}" \
        -H 'Range: bytes=0-1023' "$url")"
    [ "$code" = "206" ] || [ "$code" = "200" ]
}

egress_probe() {
    local video total=0 played=0
    for video in $EGRESS_PROBE_VIDEOS; do
        total=$((total + 1))
        if egress_probe_one "$video"; then played=$((played + 1)); fi
    done
    echo "YouTube test: $played of $total videos played."
    [ $((played * 2)) -gt "$total" ]
}

egress_ip_seen() { [ -f "$EGRESS_IP_LOG" ] && grep -qxF "$1" "$EGRESS_IP_LOG"; }

egress_remember_ip() {
    echo "$1" >> "$EGRESS_IP_LOG"
    tail -n 20 "$EGRESS_IP_LOG" > "$EGRESS_IP_LOG.tmp" && mv "$EGRESS_IP_LOG.tmp" "$EGRESS_IP_LOG"
}

egress_start_vpn() {
    local gateway
    gateway="$(egress_gateway_ip)"
    [ -n "$gateway" ] || { echo "Error. Could not find Docker's bridge address."; return 1; }
    [ -f "$EGRESS_VPN_FILE" ] || { echo "Error. No VPN settings saved. Choose the VPN option first."; return 1; }
    docker rm -f "$EGRESS_NAME" >/dev/null 2>&1 || true
    docker run -d \
        --name "$EGRESS_NAME" \
        --label "role=streamerbot-infrastructure" \
        --restart always \
        --cap-add NET_ADMIN \
        --device /dev/net/tun \
        --env-file "$EGRESS_VPN_FILE" \
        -e HTTPPROXY=on \
        -e "HTTPPROXY_LISTENING_ADDRESS=:${EGRESS_PROXY_PORT}" \
        -p "${gateway}:${EGRESS_PROXY_PORT}:${EGRESS_PROXY_PORT}" \
        "$EGRESS_IMAGE" >/dev/null || return 1
    egress_write_env vpn "http://${gateway}:${EGRESS_PROXY_PORT}"
}

# WARP keeps its registration in a volume so a restart does not register a new
# device every time. Removing the volume is how a fresh registration is forced.
egress_warp_volume() { echo "${EGRESS_NAME}-data"; }

egress_start_warp() { # [fresh]
    local gateway
    gateway="$(egress_gateway_ip)"
    [ -n "$gateway" ] || { echo "Error. Could not find Docker's bridge address."; return 1; }
    docker rm -f "$EGRESS_NAME" >/dev/null 2>&1 || true
    [ "${1:-}" != "fresh" ] || docker volume rm "$(egress_warp_volume)" >/dev/null 2>&1 || true
    docker run -d \
        --name "$EGRESS_NAME" \
        --label "role=streamerbot-infrastructure" \
        --restart always \
        --cap-add NET_ADMIN \
        --device /dev/net/tun \
        --sysctl net.ipv6.conf.all.disable_ipv6=0 \
        --sysctl net.ipv4.conf.all.src_valid_mark=1 \
        -v "$(egress_warp_volume):/var/lib/cloudflare-warp" \
        -p "${gateway}:${EGRESS_PROXY_PORT}:${WARP_INTERNAL_PORT}" \
        "$WARP_IMAGE" >/dev/null || return 1
    egress_write_env warp "http://${gateway}:${EGRESS_PROXY_PORT}"
}

egress_setup_warp() {
    local ip
    echo ""
    echo "Cloudflare WARP. Free, and it needs no account."
    echo "It sends YouTube traffic out through a Cloudflare address instead of this server's."
    echo "Cloudflare addresses are shared, so YouTube may refuse them too. The test will say."
    echo "Starting the WARP container. This can take up to a minute."
    egress_start_warp || return 1
    if ! ip="$(egress_wait_for_ip)"; then
        echo "Error. WARP did not come up. Last log lines:"
        docker logs --tail 15 "$EGRESS_NAME" 2>&1
        return 1
    fi
    egress_remember_ip "$ip"
    echo "OK. WARP is up. YouTube will see this address: $ip"
    egress_apply
}

# Wait for the tunnel to carry traffic; prints the exit address.
egress_wait_for_ip() {
    local i ip
    for i in $(seq 1 30); do
        ip="$(egress_public_ip)"
        if [ -n "$ip" ]; then echo "$ip"; return 0; fi
        sleep 2
    done
    return 1
}

# Recreate the bridge and every bot so they pick up YOUTUBE_PROXY_URL.
# Containers only read their environment at creation.
egress_apply() {
    echo "Applying the new setting. Bots stop briefly while they are recreated."
    create_shared_youtube_service || return 1
    start_shared_youtube_service || return 1
    recreate_bot_containers
    cli_for_each_bot start
}

egress_setup_vpn() {
    local provider vpntype user pass key addr countries
    echo ""
    echo "VPN container (gluetun). It needs an account with a VPN provider."
    echo "Use the provider's name as gluetun spells it, for example: mullvad,"
    echo "protonvpn, nordvpn, surfshark, privateinternetaccess, windscribe."
    read -r -p "Provider: " provider
    [ -n "$provider" ] || { echo "Cancelled. With no provider account, choose 'I need a VPN account' from the menu."; return 1; }
    echo "1. OpenVPN (username and password)"
    echo "2. WireGuard (private key)"
    read -r -p "Protocol [1]: " vpntype
    read -r -p "Countries to pick servers from, comma separated (Enter = any): " countries
    (
        umask 077
        {
            echo "VPN_SERVICE_PROVIDER=$provider"
            if [ "$vpntype" = "2" ]; then
                read -r -s -p "WireGuard private key: " key; echo >&2
                read -r -p "WireGuard address(es), if your provider gives one (Enter = none): " addr
                echo "VPN_TYPE=wireguard"
                echo "WIREGUARD_PRIVATE_KEY=$key"
                [ -z "$addr" ] || echo "WIREGUARD_ADDRESSES=$addr"
            else
                read -r -p "VPN username: " user
                read -r -s -p "VPN password: " pass; echo >&2
                echo "VPN_TYPE=openvpn"
                echo "OPENVPN_USER=$user"
                echo "OPENVPN_PASSWORD=$pass"
            fi
            [ -z "$countries" ] || echo "SERVER_COUNTRIES=$countries"
        } > "$EGRESS_VPN_FILE"
    ) || return 1
    chmod 600 "$EGRESS_VPN_FILE"

    echo "Starting the VPN container."
    egress_start_vpn || return 1
    local ip
    if ip="$(egress_wait_for_ip)"; then
        egress_remember_ip "$ip"
        echo "OK. The VPN is up. YouTube will see this address: $ip"
    else
        echo "Error. The VPN did not come up. Last log lines:"
        docker logs --tail 15 "$EGRESS_NAME" 2>&1
        return 1
    fi
    egress_apply
}

egress_setup_url() {
    local url
    echo ""
    echo "Your own proxy, for example a residential proxy."
    echo "Format: http://user:password@host:port"
    read -r -p "Proxy URL: " url
    case "$url" in
        http://*|https://*) ;;
        *) echo "Error. The address must start with http:// or https://."; return 1 ;;
    esac
    docker rm -f "$EGRESS_NAME" >/dev/null 2>&1 || true
    egress_write_env url "$url"
    local ip
    ip="$(egress_public_ip)"
    if [ -z "$ip" ]; then
        echo "Error. That proxy did not answer. Nothing was changed."
        egress_write_env direct ""
        return 1
    fi
    echo "OK. YouTube will see this address: $ip"
    egress_apply
}

egress_setup_direct() {
    docker rm -f "$EGRESS_NAME" >/dev/null 2>&1 || true
    egress_write_env direct ""
    egress_apply
}

# Move to a different exit address, for a VPN or for WARP.
#
# A VPN (gluetun) picks a server at random from those matching its filters each
# time it starts, so a restart usually changes the address. WARP is different:
# a brand-new registration gave the same address, so for WARP this tries twice
# and then says so instead of claiming a switch. Either way an address already
# tried is not accepted, and a new one is proved against YouTube before this
# reports success.
egress_rotate() {
    local mode tries try ip got_new=0
    mode="$(egress_mode)"
    case "$mode" in
        vpn)  tries="$EGRESS_ROTATE_TRIES" ;;
        warp) tries="$WARP_ROTATE_TRIES" ;;
        *)
            echo "Switching address needs the VPN or WARP container. Current setting: $mode."
            return 1
            ;;
    esac
    for try in $(seq 1 "$tries"); do
        echo "Attempt $try of $tries: asking for a different address."
        if [ "$mode" = "warp" ]; then
            egress_start_warp fresh || { echo "Error. Could not restart WARP."; return 1; }
        else
            docker restart "$EGRESS_NAME" >/dev/null 2>&1 || { echo "Error. Could not restart the VPN container."; return 1; }
        fi
        if ! ip="$(egress_wait_for_ip)"; then
            echo "The container did not come back up."
            continue
        fi
        if egress_ip_seen "$ip"; then
            echo "Got $ip, which was already used. Trying again."
            continue
        fi
        egress_remember_ip "$ip"
        got_new=1
        # The bridge keeps connections open through the old tunnel.
        docker restart "$YOUTUBE_SERVICE_NAME" >/dev/null 2>&1 || true
        sleep 5
        if egress_probe; then
            echo "OK. New address $ip, and YouTube plays through it."
            return 0
        fi
        echo "New address $ip, but YouTube still refuses it."
    done
    echo "Error. No working new address after $tries attempts."
    if [ "$got_new" -eq 0 ]; then
        echo "Every attempt gave an address that was already used."
    else
        echo "YouTube refused the new address it was given."
    fi
    if [ "$mode" = "warp" ]; then
        echo "WARP often hands this server the same few addresses. A VPN gives more choice:"
        echo "choose the VPN option, or 'I need a VPN account' if you do not have one."
    fi
    return 1
}

# For cron or the menu: do nothing if YouTube plays, rotate only if it does not.
egress_check() {
    if egress_probe; then
        echo "OK. YouTube plays through the current setting ($(egress_mode))."
        return 0
    fi
    echo "YouTube refused the test video."
    case "$(egress_mode)" in
        vpn|warp) egress_rotate ;;
        *)
            echo "Setting: $(egress_mode). Choose WARP, a VPN or a proxy from the YouTube egress menu."
            return 1
            ;;
    esac
}

egress_status() {
    local mode ip
    mode="$(egress_mode)"
    echo "YouTube egress: $mode"
    case "$mode" in
        warp|vpn|url)
            ip="$(egress_public_ip)"
            echo "Exit address: ${ip:-not answering}"
            ;;
        *) echo "Traffic leaves from this server's own address." ;;
    esac
}

# For someone with no VPN provider. Plain numbered lines, one idea each, so a
# screen reader reads them in order. Provider details change; the last line
# points at the source that is kept current.
egress_account_help() {
    echo ""
    echo "Getting a VPN account for the VPN container"
    echo ""
    echo "A VPN account is a login with a company that rents out server addresses."
    echo "The container signs in with it, and YouTube then sees the company's address"
    echo "instead of this server's. If you would rather not pay or sign up, choose"
    echo "Cloudflare WARP from the menu. It is free and needs no account."
    echo ""
    echo "1. Pick a provider. Three that work with the container:"
    echo "   Mullvad (paid, no email needed), ProtonVPN (has a free plan),"
    echo "   NordVPN (paid)."
    echo "2. Mullvad: on mullvad.net choose to generate an account number, then add"
    echo "   time to it. In the account's WireGuard section, generate a key and note"
    echo "   the private key and the address that goes with it."
    echo "   In the VPN option, type: mullvad, choose WireGuard, then enter the key"
    echo "   and the address."
    echo "3. ProtonVPN: create an account at proton.me, then in the account's"
    echo "   downloads page find the OpenVPN username and password. They are not"
    echo "   your login. In the VPN option, type: protonvpn, choose OpenVPN."
    echo "4. NordVPN: sign in to the Nord account page and look for the manual"
    echo "   setup service credentials. They are not your login. In the VPN option,"
    echo "   type: nordvpn, choose OpenVPN."
    echo "5. Other providers work if gluetun supports them. Its wiki lists each"
    echo "   provider's exact name and what to enter:"
    echo "   github.com/qdm12/gluetun-wiki"
    echo ""
    echo "A VPN address can be refused by YouTube too. That is why the menu can"
    echo "switch to a different address, and test that YouTube plays before it says so."
}

youtube_egress_menu() {
    local choice
    while true; do
        echo ""
        echo "YouTube egress"
        egress_status
        echo ""
        echo "1. Direct, no proxy (this server's own address)"
        echo "2. Cloudflare WARP (free, no account)"
        echo "3. VPN container (needs an account with a VPN provider)"
        echo "4. My own proxy address"
        echo "5. I need a VPN account. Show me how to get one"
        echo "6. Switch to a different address now (VPN or WARP)"
        echo "7. Test whether YouTube plays, and switch address if it does not"
        echo "0. Return"
        read -r -p "Choose an option: " choice
        case "$choice" in
            1) egress_setup_direct ;;
            2) egress_setup_warp ;;
            3) egress_setup_vpn ;;
            4) egress_setup_url ;;
            5) egress_account_help ;;
            6) egress_rotate ;;
            7) egress_check ;;
            0) return 0 ;;
            *) ;;
        esac
    done
}
