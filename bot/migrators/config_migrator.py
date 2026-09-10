import sys

from bot.config import ConfigManager, config_data_type


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
    if "config_version" not in config_data:
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
