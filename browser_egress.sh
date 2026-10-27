#!/bin/bash
# Browser egress: send the bots' Disney Plus and Paramount Plus browser out
# through a residential proxy, because both refuse a data-centre address.
#
# Sourced by streamerbot.sh, which supplies SCRIPT_DIR and the helpers used in
# browser_proxy_apply. Only defines functions; nothing runs on source.
#
# Measured from an OVH server on 2026-10-27: Disney Plus answers the page's
# first call (registerDevice) with "Forbidden location", so there is no sign-in
# form at all, and Paramount Plus answers 406 Not Acceptable for every page.
# From a home connection both load normally. Signing in on another computer and
# copying the session over does not help: every Disney token goes through the
# same service that refuses the address.
#
# File, untracked and holding credentials (0600):
#   browser_proxy.env   BROWSER_PROXY_URL and BROWSER_PROXY_SERVICES

BROWSER_PROXY_FILE="$SCRIPT_DIR/browser_proxy.env"
BROWSER_PROXY_DEFAULT_SERVICES="dp,pp"

browser_proxy_write_env() { # url services
    ( umask 077
      printf 'BROWSER_PROXY_URL=%s\nBROWSER_PROXY_SERVICES=%s\n' "$1" "$2" > "$BROWSER_PROXY_FILE" )
    BROWSER_PROXY_URL="$1"
    BROWSER_PROXY_SERVICES="$2"
}

# The proxy's address with the password left out, for showing on screen.
browser_proxy_safe_url() {
    echo "$1" | sed -E 's#//[^@/]*@#//#'
}

# "address, company" as seen through the proxy, or nothing if it does not answer.
browser_proxy_exit() { # url
    curl -fsS --max-time 20 -x "$1" https://ipinfo.io/json 2>/dev/null \
        | jq -r '"\(.ip), \(.org // "unknown company"), \(.country // "?")"' 2>/dev/null
}

# Paramount Plus answers 406 to a refused address before any page loads, so a
# plain request is a real test of the proxy. Disney's refusal happens inside the
# page's own scripts, which only the bot's browser runs.
browser_proxy_paramount_status() { # url
    curl -s -o /dev/null --max-time 30 -x "$1" -w '%{http_code}' \
        -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0 Safari/537.36" \
        https://www.paramountplus.com/account/signin/ 2>/dev/null
}

browser_proxy_status() {
    if [ -z "${BROWSER_PROXY_URL:-}" ]; then
        echo "Disney Plus and Paramount Plus connect directly, from this server's own address."
        echo "Both refuse data-centre addresses, so they will not sign in from here without a proxy."
        return 0
    fi
    echo "Proxy: $(browser_proxy_safe_url "$BROWSER_PROXY_URL")"
    echo "Services that use it: ${BROWSER_PROXY_SERVICES:-$BROWSER_PROXY_DEFAULT_SERVICES}"
}

browser_proxy_help() {
    echo ""
    echo "Choosing a residential proxy for Disney Plus and Paramount Plus"
    echo ""
    echo "The bot's browser downloads the whole video even though only the sound is sent to TeamTalk. That is about 1 to 3 GB for each hour watched."
    echo "So choose a static residential proxy, also called an ISP proxy, sold per address with unlimited or large traffic."
    echo "Avoid plans sold per gigabyte: at 3 to 7 dollars a gigabyte, one film can cost 5 to 20 dollars."
    echo "A static address also stays the same, which Disney Plus prefers to an address that changes on every request."
    echo ""
    echo "Pick an address in the same country as the streaming account. A US account needs a US address."
    echo "One address is enough for every bot on this server."
    echo ""
    echo "Three providers with plans that fit, at their listed prices in October 2026. Check the price before buying:"
    echo ""
    echo "1. IPRoyal, static residential proxies. iproyal.com"
    echo "   About 2.70 dollars an address for 30 days, unlimited traffic, address only yours."
    echo "   Has US and Canadian addresses. A 24 hour plan for about 1.80 dollars lets you test first."
    echo "2. Proxy-Seller, ISP proxies. proxy-seller.com"
    echo "   From about 1 dollar an address a month, address only yours, username and password sign-in."
    echo "   Its fair use policy allows about 100 GB an address a month. US, but no Canadian addresses."
    echo "3. Webshare, static residential proxies. webshare.io"
    echo "   About 30 cents an address, but the smallest plan is 20 addresses, about 6 dollars a month."
    echo "   Traffic starts at 250 GB a month under a fair use policy."
    echo ""
    echo "After buying, find the proxy's address in the provider's dashboard."
    echo "Choose the HTTP type with a username and password, not SOCKS5: Chrome cannot sign in to a SOCKS5 proxy."
    echo "It looks like this: http://username:password@host:port"
    echo "Then choose 'Set the proxy' from this menu and paste it."
    echo ""
    echo "None of these has been tried with Disney Plus yet. If Disney still refuses the address, the bot says so, and the provider's own support can usually swap it."
}

# Recreate every bot so it picks up the new setting. Containers only read their
# environment when they are created.
browser_proxy_apply() {
    echo "Applying the new setting. Bots stop briefly while they are recreated."
    recreate_bot_containers
    cli_for_each_bot start
}

browser_proxy_setup() {
    local url services exit_info code
    echo ""
    echo "Paste the proxy address from your provider."
    echo "Format: http://username:password@host:port"
    echo "If you do not have one yet, choose 'Which proxy should I buy?' from this menu."
    read -r -p "Proxy address: " url
    case "$url" in
        http://*|https://*) ;;
        socks5://*) echo "Error. Chrome cannot sign in to a SOCKS5 proxy. Use the provider's HTTP address instead."; return 1 ;;
        "") echo "Cancelled. Nothing was changed."; return 1 ;;
        *) echo "Error. The address must start with http:// or https://."; return 1 ;;
    esac

    echo "Testing the proxy."
    exit_info="$(browser_proxy_exit "$url")"
    if [ -z "$exit_info" ]; then
        echo "Error. That proxy did not answer. Check the address, username and password. Nothing was changed."
        return 1
    fi
    echo "OK. The streaming services will see this address: $exit_info"
    code="$(browser_proxy_paramount_status "$url")"
    case "$code" in
        2*|3*) echo "OK. Paramount Plus accepts this address." ;;
        406|403) echo "Warning. Paramount Plus refused this address too (HTTP $code). Ask the provider for a different one, or keep it for Disney Plus only." ;;
        *) echo "Paramount Plus could not be checked (HTTP ${code:-no answer})." ;;
    esac

    echo ""
    echo "Which services should use the proxy?"
    echo "Press Enter for Disney Plus and Paramount Plus (dp,pp)."
    echo "Or type service names separated by commas, for example dp,pp,hu to add Hulu."
    read -r -p "Services [$BROWSER_PROXY_DEFAULT_SERVICES]: " services
    services="$(echo "${services:-$BROWSER_PROXY_DEFAULT_SERVICES}" | tr -d ' ')"
    case "$services" in
        *[!a-z,\*]*) echo "Error. Use service names like dp,pp separated by commas. Nothing was changed."; return 1 ;;
    esac

    browser_proxy_write_env "$url" "$services"
    echo "OK. Saved. Each bot can now sign in to these services again with li dp or li pp."
    browser_proxy_apply
}

browser_proxy_off() {
    rm -f "$BROWSER_PROXY_FILE"
    BROWSER_PROXY_URL=""
    BROWSER_PROXY_SERVICES=""
    echo "OK. The proxy is off. Disney Plus and Paramount Plus connect directly again."
    browser_proxy_apply
}

browser_egress_menu() {
    local choice
    while true; do
        echo ""
        echo "Disney Plus and Paramount Plus proxy"
        browser_proxy_status
        echo ""
        echo "1. Which proxy should I buy? (providers and prices)"
        echo "2. Set the proxy"
        echo "3. Turn the proxy off"
        echo "0. Return"
        read -r -p "Choose an option: " choice
        case "$choice" in
            1) browser_proxy_help ;;
            2) browser_proxy_setup ;;
            3) browser_proxy_off ;;
            0) return 0 ;;
            *) ;;
        esac
    done
}
