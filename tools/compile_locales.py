#!/usr/bin/env python3

import os
import sys
import subprocess


cd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
locale_path = os.path.join(cd, "locale")
pot_file_path = os.path.join(locale_path, "StreamerBot.pot")
source_paths = [os.path.join(cd, "bot"), os.path.join(cd, "StreamerBot.py")]
babel_prefix = "{} -m babel.messages.frontend".format(sys.executable)
locale_domain = "StreamerBot"


def extract():
    code = subprocess.call(
        f"{babel_prefix} extract {' '.join(source_paths)} -o {pot_file_path} --keywords=translate -c translators: --copyright-holder=StreamerBot-team --project=StreamerBot",
        shell=True,
    )
    if code:
        sys.exit("Bable is not installed. please install all the requirements")


def update():
    code = subprocess.call(
        f"{babel_prefix} update -i {pot_file_path} -d {locale_path} -D {locale_domain} --update-header-comment --previous",
        shell=True,
    )
    if code:
        sys.exit(code)


def compile():
    code = subprocess.call(
        f"{babel_prefix} compile -d {locale_path} -D {locale_domain}", shell=True
    )
    if code:
        sys.exit(code)


def main():
    extract()
    update()
    compile()


if __name__ == "__main__":
    main()
