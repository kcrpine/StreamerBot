#!/bin/bash

set -e

PROGNAME=StreamerBot.py
PROGDIR=$(dirname "$(readlink -f "$0")")

# /opt/teamtalk is where the Dockerfile unpacks the TeamTalk SDK. The two
# in-tree paths are kept for bare-metal installs that drop TeamTalk_DLL next to
# the bot, which is how the pre-Docker install scripts laid it out.
LD_LIBRARY_PATH=/opt/teamtalk:$PROGDIR/TeamTalk_DLL:$PROGDIR:$LD_LIBRARY_PATH
export LD_LIBRARY_PATH

exec python3 "$PROGDIR/$PROGNAME" "$@"
