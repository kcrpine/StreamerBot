from __future__ import annotations
import os
from typing import Callable, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from bot.translator import Translator

directory = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_project_env(path: str = "") -> Dict[str, str]:
    """Parse project.env, the shell-sourceable file the deployment scripts share.

    Deliberately stdlib-only and forgiving: a missing or malformed file must never
    stop the bot from starting, it just means the defaults below are used.
    """
    values: Dict[str, str] = {}
    path = path or os.path.join(directory, "project.env")
    try:
        with open(path, "r", encoding="UTF-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


project_env = load_project_env()

app_name = "StreamerBot"
app_version = "1.0"
client_name = app_name + "-V" + app_version

repo_owner = project_env.get("STREAMERBOT_REPO_OWNER", "") or "kcrpine"
repo_name = project_env.get("STREAMERBOT_REPO_NAME", "") or "StreamerBot"
repo_url = f"https://github.com/{repo_owner}/{repo_name}"
ttsdk_version = project_env.get("TTSDK_VERSION", "") or "5.22a"

about_text: Callable[[Translator], str] = lambda translator: translator.translate(
    """\
StreamerBot for TeamTalk 5.
Streams YouTube, YouTube Music, Spotify, Apple Music, Amazon Music, Netflix,
Disney Plus and direct links into a channel, with audio description support.
Repository: {repo}
TeamTalk SDK {sdk}.
Based on TTMediaBot by Amir Gumerov, Vladislav Kopylov, Beqa Gozalishvili and
Kirill Belousov, and on the fork by Joao Almeida.
"""
).format(repo=repo_url, sdk=ttsdk_version)

fallback_service = "yt"
loop_timeout = 0.01
max_message_length = 256
recents_max_lenth = 32
tt_event_timeout = 2
