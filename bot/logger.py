from __future__ import annotations
from enum import Flag
import logging
from logging.handlers import RotatingFileHandler
import os
import threading
import sys
from typing import TYPE_CHECKING, Any, List

from bot import app_vars

if TYPE_CHECKING:
    from bot import Bot


class Mode(Flag):
    STDOUT = 1
    FILE = 2
    STDOUT_AND_FILE = STDOUT | FILE


def initialize_logger(bot: Bot) -> None:
    config = bot.config.logger
    logging.addLevelName(5, "PLAYER_DEBUG")
    level = logging.getLevelName(config.level)
    formatter = logging.Formatter(config.format)
    handlers: List[Any] = []
    try:
        mode = (
            Mode(config.mode)
            if isinstance(config.mode, int)
            else Mode.__members__[config.mode]
        )
    except KeyError:
        sys.exit("Invalid log mode name")
    if mode & Mode.FILE == Mode.FILE:
        if bot.log_file_name:
            file_name = bot.log_file_name
        else:
            file_name = config.file_name
        if os.path.isdir(os.path.join(*os.path.split(file_name)[0:-1])):
            file = file_name
        else:
            file = os.path.join(bot.config_manager.config_dir, file_name)
        rotating_file_handler = RotatingFileHandler(
            filename=file,
            mode="a",
            maxBytes=config.max_file_size * 1024,
            backupCount=config.backup_count,
            encoding="UTF-8",
        )
        rotating_file_handler.setFormatter(formatter)
        rotating_file_handler.setLevel(level)
        handlers.append(rotating_file_handler)
    if mode & Mode.STDOUT == Mode.STDOUT:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(level)
        handlers.append(stream_handler)
    logging.basicConfig(level=level, format=config.format, handlers=handlers)
    # From here on, nothing crashes silently: an unhandled exception in any
    # of the bot's seventeen threads reaches this log rather than stderr.
    install_exception_hooks()

def install_exception_hooks() -> None:
    """Make sure no unhandled exception escapes without reaching the log.

    Python's defaults print a traceback to stderr. In a container with the logger
    in FILE mode that goes to docker logs at best and nowhere at worst, so the log
    file a user is asked to send would show the bot working right up to the moment
    it stopped, with no reason recorded.

    This matters more here than in most programs because the bot runs seventeen
    threads: the mpv event thread, the browser worker, the librespot monitor, one
    per command, the task processor. threading.excepthook covers those, and
    sys.excepthook the main thread. A thread dying silently is how "playback just
    stops" bugs become unreportable.

    Everything goes through logging rather than print, so the secret-redaction
    filter scrubs it on the way out: a traceback can carry a password in a local
    variable's repr.
    """

    def handle_main(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            # Ctrl+C is a request, not a fault. Restore the default behaviour so
            # the process still exits promptly.
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.critical(
            "Unhandled exception; the bot is stopping",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def handle_thread(args) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        name = getattr(args.thread, "name", "unknown")
        logging.error(
            f"Unhandled exception in thread {name}; that thread has died",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def handle_unraisable(args) -> None:
        # Raised from __del__ and similar, where an exception cannot propagate.
        # Usually harmless, occasionally the only sign a handle was left open.
        logging.warning(
            f"Unraisable exception in {args.object!r}",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = handle_main
    threading.excepthook = handle_thread
    sys.unraisablehook = handle_unraisable
