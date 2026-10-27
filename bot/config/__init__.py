import json
import os
import stat
import sys
import tempfile
import threading
from typing import Any, Dict, Optional, Sequence

from bot import app_vars, utils
from bot.config.models import ConfigModel
from bot.migrators import config_migrator

import portalocker

config_data_type = Dict[str, Any]


def save_default_file() -> None:
    with open(utils.get_abs_path("config_default.json"), "w") as f:
        json.dump(ConfigModel().model_dump(), f, indent=4, ensure_ascii=False)


class ConfigManager:
    version = 3

    def __init__(self, file_name: Optional[str]) -> None:
        self._save_lock = threading.Lock()
        if file_name:
            if os.path.isfile(file_name):
                self.file_name = os.path.abspath(file_name)
                config_dict = config_migrator.migrate(self, self._load())
                self.config_dir = os.path.dirname(self.file_name)
                self._lock()
            else:
                sys.exit("Incorrect configuration file path")
        else:
            self.config_dir = app_vars.directory
            config_dict = {}
        self.config: ConfigModel = ConfigModel(**config_dict)

    def _dump(self, data: config_data_type):
        with open(self.file_name, "w", encoding="UTF-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    def _load(self):
        with open(self.file_name, "r", encoding="UTF-8") as f:
            try:
                return json.load(f)
            except json.decoder.JSONDecodeError as e:
                sys.exit("Syntax error in configuration file: " + str(e))

    def _lock(self):
        self.file_locker = portalocker.Lock(
            self.file_name,
            timeout=0,
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
        )
        try:
            self.file_locker.acquire()
        except portalocker.exceptions.LockException:
            raise PermissionError()

    def close(self):
        self.file_locker.release()

    def save(self):
        self.file_locker.release()
        self._dump(self.config.model_dump())
        self.file_locker.acquire()

    def save_setting(self, path: Sequence[str], value: Any) -> None:
        """Write one setting to config.json and leave every other key as it is on disk.

        save() writes the whole in-memory model. That silently undoes an edit
        made with streamerbot.sh while the bot was running, and saves whatever
        else was changed in memory without being meant to stick. The config
        command saves each change on its own, so it writes only that change.
        The file is replaced atomically, because a bot killed half way through
        a plain write leaves a config.json it cannot start from.
        """
        with self._save_lock:
            file_name = getattr(self, "file_name", None)
            if not file_name:
                return
            locker = getattr(self, "file_locker", None)
            if locker is not None:
                locker.release()
            try:
                with open(file_name, "r", encoding="UTF-8") as f:
                    data = json.load(f)
                node = data
                for key in path[:-1]:
                    child = node.get(key)
                    if not isinstance(child, dict):
                        child = {}
                        node[key] = child
                    node = child
                node[path[-1]] = value
                directory = os.path.dirname(file_name)
                fd, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
                try:
                    with os.fdopen(fd, "w", encoding="UTF-8") as f:
                        json.dump(data, f, indent=4, ensure_ascii=False)
                    try:
                        os.chmod(temp_name, stat.S_IMODE(os.stat(file_name).st_mode))
                    except OSError:
                        pass
                    os.replace(temp_name, file_name)
                except BaseException:
                    try:
                        os.remove(temp_name)
                    except OSError:
                        pass
                    raise
            finally:
                if locker is not None:
                    locker.acquire()
