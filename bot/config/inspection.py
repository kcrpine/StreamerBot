"""Read-only inspection of a bot's configuration file.

Nothing here writes, and nothing here takes the portalocker lock that
ConfigManager takes. A running bot holds that lock on its own config, and a
check that cannot be run while the bots are up is a check nobody runs.

The rules live here, beside the model and the migration table that define them,
and streamerbot.sh asks this module rather than restating the same rules in jq.
config.json declaring config_version 2 while ConfigManager understood 1 is
exactly what happens when two copies of the rules drift apart, and that one
stopped every newly created bot before it reached TeamTalk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

from pydantic import ValidationError

from bot.config import ConfigManager
from bot.config.models import ConfigModel, ServicesModel, TeamTalkModel
from bot.migrators.config_migrator import is_foreign_lineage, migrate_functs

# The bot will not start at all.
ERROR = "error"
# The bot starts, but not as whoever configured it intended.
WARNING = "warning"
# Worth saying out loud. Nothing is wrong.
NOTE = "note"

SEVERITY_ORDER = {ERROR: 0, WARNING: 1, NOTE: 2}

# The services this bot actually has. Read from the model rather than typed out,
# so adding a service cannot leave this list behind.
SERVICE_NAMES = tuple(
    name for name in ServicesModel.model_fields if name != "default_service"
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    # One plain sentence. It is read aloud, so it says what is wrong rather
    # than naming an exception class.
    message: str
    remedy: str = ""


def inspect_config_file(path: str) -> List[Finding]:
    """Inspect one config.json. Never raises for a bad file; it reports."""
    try:
        with open(path, "r", encoding="UTF-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return [
            Finding(
                ERROR,
                "missing",
                "There is no config.json in this bot's folder.",
                "Create the bot again, or restore it from a backup.",
            )
        ]
    except json.JSONDecodeError as error:
        return [
            Finding(
                ERROR,
                "unreadable",
                f"config.json is not valid JSON: {error}.",
                "Fix the punctuation on that line, or restore from a backup.",
            )
        ]
    except OSError as error:
        return [
            Finding(ERROR, "unreadable", f"config.json could not be read: {error}.")
        ]
    return inspect_config_data(data)


def inspect_config_data(data: Any) -> List[Finding]:
    findings: List[Finding] = []
    if not isinstance(data, dict):
        return [
            Finding(
                ERROR,
                "not_an_object",
                "config.json does not contain a configuration object.",
            )
        ]

    # Several checks have to run against what the bot will actually load, not
    # against the file as written: reporting a problem that starting the bot
    # would have silently fixed trains people to ignore the report.
    migrated = _as_loaded(data)

    findings.extend(_check_version(data))
    findings.extend(_check_lineage(data))
    findings.extend(_check_default_service(migrated))
    findings.extend(_check_model(migrated))
    findings.extend(_check_reachability(data))
    findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    return findings


def _check_version(data: Dict[str, Any]) -> List[Finding]:
    current = ConfigManager.version
    if "config_version" not in data:
        return [
            Finding(
                NOTE,
                "version_missing",
                "This configuration predates version numbering. It is brought up "
                f"to version {current} when the bot starts.",
            )
        ]
    version = data["config_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        return [
            Finding(
                ERROR,
                "version_not_a_number",
                f"config_version is {version!r}, which is not a whole number. The "
                "bot stops at startup rather than guess.",
            )
        ]
    if version > current:
        return [
            Finding(
                ERROR,
                "version_too_new",
                f"This configuration is version {version}, but the bot only "
                f"understands up to version {current}. The bot stops at startup, "
                "before it reaches TeamTalk.",
                "Update the bot, or restore the configuration it was created with.",
            )
        ]
    if version < current:
        return [
            Finding(
                NOTE,
                "needs_migration",
                f"This configuration is version {version} and is brought up to "
                f"version {current} when the bot starts.",
            )
        ]
    return []


def _check_lineage(data: Dict[str, Any]) -> List[Finding]:
    if not is_foreign_lineage(data):
        return []
    return [
        Finding(
            WARNING,
            "foreign_lineage",
            "This configuration came from TTMediaBot or one of its other forks. "
            "Its version number does not mean the same thing here, so it is "
            "migrated on the way in whatever number it carries.",
            "Check the nickname, the server and the channel afterwards.",
        )
    ]


def _check_default_service(data: Dict[str, Any]) -> List[Finding]:
    services = data.get("services")
    if not isinstance(services, dict):
        return []
    default = services.get("default_service")
    if default is None or default in SERVICE_NAMES:
        return []
    # ServiceManager looks this up in a plain dict, so a name that is not there
    # is a KeyError during Bot.__init__ -- the bot dies before connecting, with
    # a traceback rather than an explanation. "vk" and "yam" arrive this way
    # from TTMediaBot configs constantly.
    return [
        Finding(
            ERROR,
            "unknown_default_service",
            f"The default service is {default!r}, which this bot does not have. "
            "The bot stops at startup, before it reaches TeamTalk.",
            "Set services.default_service to one of: " + ", ".join(SERVICE_NAMES) + ".",
        )
    ]


def _as_loaded(data: Dict[str, Any]) -> Dict[str, Any]:
    """The configuration as the bot will have it, after migration.

    Works on a copy and writes nothing: a running bot owns its own file.
    """
    migrated = json.loads(json.dumps(data))
    version = migrated.get("config_version")
    if not isinstance(version, int) or isinstance(version, bool):
        version = 0
    if is_foreign_lineage(migrated):
        version = 0
    for ver in sorted(migrate_functs):
        if ver > version:
            try:
                migrated = migrate_functs[ver](migrated)
            except Exception:
                # A migration that cannot run on this file is itself the
                # finding; _check_model reports what the model then rejects.
                break
    return migrated


def _check_model(migrated: Dict[str, Any]) -> List[Finding]:
    """Run the migrated configuration past the real model."""
    try:
        ConfigModel(**migrated)
    except ValidationError as error:
        return [
            Finding(
                ERROR,
                "invalid_value",
                "{} is not valid: {}.".format(
                    ".".join(str(part) for part in item["loc"]), item["msg"]
                ),
            )
            for item in error.errors()
        ]
    return []


# Names for this same machine. Containers are created with --network host, so
# these reach the host's own ports: a TeamTalk server on the same box is an
# ordinary way to run this, not a mistake.
LOCAL_HOSTNAMES = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _check_reachability(data: Dict[str, Any]) -> List[Finding]:
    """Things that let a bot start and then never appear in the channel."""
    teamtalk = data.get("teamtalk")
    if not isinstance(teamtalk, dict):
        return []
    findings = []
    hostname = teamtalk.get("hostname", "")
    username = teamtalk.get("username", "")
    nickname = teamtalk.get("nickname", "")

    # Read from the model, so changing a default cannot leave this behind.
    default_hostname = TeamTalkModel.model_fields["hostname"].default
    default_nickname = TeamTalkModel.model_fields["nickname"].default

    if not hostname:
        findings.append(
            Finding(
                WARNING,
                "no_server",
                "No server is set, so the bot has nothing to connect to. It "
                "starts and retries forever without appearing in a channel.",
                "Set teamtalk.hostname to the server's address.",
            )
        )
    elif hostname in LOCAL_HOSTNAMES:
        findings.append(
            Finding(
                NOTE,
                "local_server",
                f"The server is {hostname}, on this same machine. That works: "
                "the container shares the host's network.",
            )
        )

    # The shipped template, untouched. Every field creating a bot fills in is
    # still at its default, which is a bot nobody finished setting up rather
    # than one pointed at a local server. Testing the hostname alone would
    # accuse every legitimate same-box deployment.
    if (
        hostname == default_hostname
        and not username
        and nickname == default_nickname
    ):
        findings.append(
            Finding(
                WARNING,
                "never_configured",
                "This bot still has the server, account and nickname it was "
                "created with, so nothing was ever filled in. It starts and "
                "never appears in a channel.",
                "Create the bot again, or edit teamtalk in its config.json.",
            )
        )

    if not nickname:
        findings.append(
            Finding(
                WARNING,
                "no_nickname",
                "This bot has no nickname, so it joins the channel unnamed.",
            )
        )
    return findings


def worst_severity(findings: List[Finding]) -> str:
    """The most serious severity present, or NOTE when there is nothing."""
    if not findings:
        return NOTE
    return min((f.severity for f in findings), key=lambda s: SEVERITY_ORDER[s])
