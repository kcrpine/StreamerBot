#!/usr/bin/env python3

import os
import sys
import subprocess


# Paths are relative and every babel command runs with the repository root as
# its working directory, so the `#:` source references in the .pot and .po files
# come out the same on every machine. Absolute paths were written into them
# verbatim: catalogs generated inside the image said `/work/bot/__init__.py` and
# the same command on a developer's host rewrote all 390 of them to that host's
# own path, which is a diff nobody can read and a local path committed to the
# repository. Babel also wraps a path containing a space in bidi isolate
# characters, so on such a host the references stopped being plain text as well.
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
locale_path = os.path.join("locale")
pot_file_path = os.path.join(locale_path, "StreamerBot.pot")
source_paths = ["bot", "StreamerBot.py"]
# A list, run without a shell. It used to be one interpolated string passed to
# shell=True, which splits on spaces: on any checkout whose path contains one,
# babel was handed half a directory name and reported "Bable is not installed"
# for a babel that was installed and working.
babel_prefix = [sys.executable, "-m", "babel.messages.frontend"]
locale_domain = "StreamerBot"


def run(*args):
    code = subprocess.call(babel_prefix + list(args), cwd=repo_root)
    if code:
        sys.exit(code)


def extract():
    code = subprocess.call(
        babel_prefix
        + [
            "extract",
            *source_paths,
            "-o",
            pot_file_path,
            "--keywords=translate",
            "-c",
            "translators:",
            "--copyright-holder=StreamerBot-team",
            "--project=StreamerBot",
        ],
        cwd=repo_root,
    )
    if code:
        sys.exit("Babel is not installed. Please install all the requirements.")


def update():
    run(
        "update",
        "-i",
        pot_file_path,
        "-d",
        locale_path,
        "-D",
        locale_domain,
        "--update-header-comment",
        "--previous",
    )


def compile():
    run("compile", "-d", locale_path, "-D", locale_domain)


def main():
    extract()
    update()
    compile()


if __name__ == "__main__":
    main()
