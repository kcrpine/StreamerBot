import logging
import sys

from bot.config import ConfigManager, config_data_type


# Service keys no StreamerBot release has ever carried. VK and Yandex Music are
# TTMediaBot's, and they arrive here whenever a configuration is restored or
# copied from that project or one of its other forks.
FOREIGN_SERVICE_KEYS = ("vk", "yam")


def is_foreign_lineage(config_data: config_data_type) -> bool:
    """True when this file was written by TTMediaBot or another of its forks.

    Version numbers are only comparable within one lineage. A fork that reached
    its own version 2 means something entirely different by it, and because
    migrate() returns early when the number already matches, such a file used to
    sail through untouched -- keeping TTMediaBot's cache and log filenames and,
    worse, a default_service of "vk", which ServiceManager looks up in a plain
    dict and dies on. So lineage is decided by shape, never by the number.
    """
    if not isinstance(config_data, dict):
        return False
    services = config_data.get("services")
    if isinstance(services, dict):
        if any(key in services for key in FOREIGN_SERVICE_KEYS):
            return True
        if services.get("default_service") in FOREIGN_SERVICE_KEYS:
            return True
    general = config_data.get("general")
    if isinstance(general, dict) and "TTMediaBot" in str(
        general.get("cache_file_name", "")
    ):
        return True
    logger = config_data.get("logger")
    if isinstance(logger, dict) and "TTMediaBot" in str(logger.get("file_name", "")):
        return True
    return False


def to_v1(config_data: config_data_type) -> config_data_type:
    return update_version(config_data, 1)


def to_v2(config_data: config_data_type) -> config_data_type:
    """StreamerBot's configuration shape.

    The new sections (services, auth_portal, audio_description) need nothing
    here: they are absent from a v1 file and pydantic fills them from the model
    defaults. What a merge cannot express is the two renames, because the
    inherited TTMediaBot values are still valid strings and would simply be
    kept. This mirrors the same migration in streamerbot.sh, which is what an
    old bot restored from a backup goes through.
    """
    general = config_data.setdefault("general", {})
    if "TTMediaBot" in general.get("cache_file_name", ""):
        general["cache_file_name"] = "StreamerBotCache.dat"
    logger = config_data.setdefault("logger", {})
    if "TTMediaBot" in logger.get("file_name", ""):
        logger["file_name"] = "StreamerBot.log"

    # Imported here rather than at module scope: bot.config imports this module
    # while it is still being defined, so a top-level import of its models would
    # be a cycle. By the time a migration actually runs, everything is loaded.
    from bot.config.models import ServicesModel

    known = [n for n in ServicesModel.model_fields if n != "default_service"]
    services = config_data.setdefault("services", {})
    if isinstance(services, dict):
        default = services.get("default_service")
        if default is not None and default not in known:
            # ServiceManager looks this up in a plain dict, so leaving another
            # fork's service name here is a KeyError during startup rather than
            # anything a user could act on. VK and Yandex Music have no
            # equivalent here, so there is nothing to map it to.
            services["default_service"] = ServicesModel.model_fields[
                "default_service"
            ].default
            logging.warning(
                "The default service was %r, which this bot does not have. "
                "It has been set to %r.",
                default,
                services["default_service"],
            )
    return update_version(config_data, 2)


# Ascending order, and every version the bot has ever written needs an entry.
# ConfigManager.version must equal the highest key here: a config_version above
# it is rejected outright, so bumping one without the other stops every bot that
# reads such a file before it reaches TeamTalk.
migrate_functs = {1: to_v1, 2: to_v2}


def migrate(
    config_manager: ConfigManager,
    config_data: config_data_type,
) -> config_data_type:
    if is_foreign_lineage(config_data):
        # Another fork's numbering. Whatever it claims, it has never been
        # through the migrations in this file, so run all of them.
        config_data = update_version(config_data, 0)
    elif "config_version" not in config_data:
        # Written before versioning existed. Call it version 0 and let every
        # migration below run, rather than returning it untouched.
        config_data = update_version(config_data, 0)
    elif not isinstance(config_data["config_version"], int):
        sys.exit(
            "Error in the configuration file: config_version must be a whole "
            f"number, not {config_data['config_version']!r}."
        )
    elif config_data["config_version"] > config_manager.version:
        # Naming both numbers matters: this is what a bot whose configuration
        # was written by a newer version of the bot looks like, and the reader
        # needs to know which side is behind.
        sys.exit(
            f"Error in the configuration file: it is version "
            f"{config_data['config_version']}, but this bot only understands up "
            f"to version {config_manager.version}. Update the bot, or restore "
            "the configuration this one was created with."
        )
    if config_data["config_version"] == config_manager.version:
        return config_data
    for ver in sorted(migrate_functs):
        if ver > config_data["config_version"]:
            config_data = migrate_functs[ver](config_data)
    config_manager._dump(config_data)
    return config_data


def update_version(config_data: config_data_type, version: int) -> config_data_type:
    _config_data = {"config_version": version}
    _config_data.update(config_data)
    _config_data["config_version"] = version
    return _config_data
