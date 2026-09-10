#!/usr/bin/env python3
"""Check bot configurations before anything tries to start them.

Run inside the image, which is the point: the rules about what a configuration
must contain live in bot/config/inspection.py, next to the model and the
migration table that define them. streamerbot.sh calls this rather than
restating those rules in jq, because two copies of the rules drifting apart is
what stopped every newly created bot reaching TeamTalk once already.

  python tools/check_config.py --bots-root /bots
  python tools/check_config.py path/to/config.json
  python tools/check_config.py --bots-root /bots --json

Exit status is 0 when nothing will stop a bot starting, 1 when something will,
and 2 when this script could not run at all. Warnings alone do not fail: a bot
pointed at the wrong server is the operator's business, not a reason to refuse.
"""

import argparse
import json
import logging
import os
import sys

# The migrations log what they repair, which is right when a bot is starting
# and noise in a report that is read aloud. This process only inspects.
logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config.inspection import (  # noqa: E402
    ERROR,
    NOTE,
    WARNING,
    Finding,
    inspect_config_file,
)

LABELS = {ERROR: "Error.", WARNING: "Warning.", NOTE: "Note."}


def collect(bots_root):
    """Every bot directory holding a config.json, in a stable order."""
    if not os.path.isdir(bots_root):
        return []
    found = []
    for name in sorted(os.listdir(bots_root)):
        directory = os.path.join(bots_root, name)
        if os.path.isdir(directory):
            found.append((name, os.path.join(directory, "config.json")))
    return found


def report_text(results, stream):
    """Plain lines, in the manager's own style.

    No colour, no progress bar and no box drawing: this is read aloud. Each
    line opens with the word Error, Warning or Note so the severity arrives
    before the detail.
    """
    errors = warnings = 0
    for name, findings in results:
        serious = [f for f in findings if f.severity in (ERROR, WARNING)]
        if not serious:
            print(f"Bot {name}: OK.", file=stream)
            continue
        print(f"Bot {name}:", file=stream)
        for finding in findings:
            if finding.severity == NOTE:
                continue
            if finding.severity == ERROR:
                errors += 1
            else:
                warnings += 1
            print(f"  {LABELS[finding.severity]} {finding.message}", file=stream)
            if finding.remedy:
                print(f"  What to do: {finding.remedy}", file=stream)
    return errors, warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check bot configurations.")
    parser.add_argument("config", nargs="?", help="A single config.json to check.")
    parser.add_argument(
        "--bots-root", help="A directory of bot folders, each with a config.json."
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="Machine-readable output."
    )
    args = parser.parse_args(argv)

    if args.config:
        results = [(os.path.basename(os.path.dirname(args.config)) or "config", 
                    inspect_config_file(args.config))]
    elif args.bots_root:
        results = [
            (name, inspect_config_file(path)) for name, path in collect(args.bots_root)
        ]
    else:
        parser.error("Give a config file or --bots-root.")
        return 2

    if args.as_json:
        json.dump(
            [
                {
                    "bot": name,
                    "findings": [vars(f) for f in findings],
                }
                for name, findings in results
            ],
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        errors = sum(
            1 for _, fs in results for f in fs if f.severity == ERROR
        )
        return 1 if errors else 0

    if not results:
        print("No bots to check.")
        return 0

    errors, warnings = report_text(results, sys.stdout)
    print("")
    if errors:
        print(
            f"Error. {errors} problem(s) will stop a bot starting. "
            "Those bots will not connect until they are fixed."
        )
        return 1
    if warnings:
        print(f"Warning. {warnings} thing(s) worth checking. Every bot will start.")
        return 0
    print(f"OK. All {len(results)} configuration(s) are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
