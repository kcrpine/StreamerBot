"""Encrypted per-bot credential storage.

What this defends against, stated honestly: a `bots/` tarball, a backup, a
support log, someone glancing at the file. The key sits next to the ciphertext,
so it does **not** defend against an attacker who already has host root or can
read the bot's data directory. Setting STREAMERBOT_SECRET_KEY supplies the key
from the environment instead, which is the real hardening path, and then the key
never touches the disk at all.

Fields are encrypted individually rather than the file as a whole, so a partial
read leaks nothing, and adding a service does not rewrite every other service's
ciphertext.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from typing import Any, Dict, Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - the package is a hard requirement
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[misc,assignment]

from bot import errors

logger = logging.getLogger(__name__)

KEY_ENV_VAR = "STREAMERBOT_SECRET_KEY"


class SecretStore:
    """Reads and writes `credentials.enc`, encrypting each field separately."""

    def __init__(self, secrets_dir: str) -> None:
        if Fernet is None:
            raise errors.ServiceError(
                "The cryptography package is required to store credentials."
            )
        self._dir = secrets_dir
        self._key_file = os.path.join(secrets_dir, "portal.key")
        self._data_file = os.path.join(secrets_dir, "credentials.enc")
        self._lock = threading.RLock()
        self._fernet = Fernet(self._load_or_create_key())

    # -- key ---------------------------------------------------------------

    def _load_or_create_key(self) -> bytes:
        env_key = os.environ.get(KEY_ENV_VAR, "").strip()
        if env_key:
            # Supplied at container start and never persisted. Validated here so
            # a malformed key fails loudly at boot rather than on first save.
            try:
                Fernet(env_key.encode())
            except Exception as error:
                raise errors.ServiceError(
                    f"{KEY_ENV_VAR} is not a valid Fernet key: {error}"
                ) from error
            logger.info("Secret key taken from the environment; nothing written to disk.")
            return env_key.encode()

        os.makedirs(self._dir, mode=0o700, exist_ok=True)
        if os.path.isfile(self._key_file):
            with open(self._key_file, "rb") as f:
                return f.read().strip()

        key = Fernet.generate_key()
        # Written 0600 before any content reaches it.
        fd = os.open(self._key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        logger.info("Generated a new credential key.")
        return key

    # -- storage -----------------------------------------------------------

    def _read_all(self) -> Dict[str, Any]:
        try:
            with open(self._data_file, "r", encoding="utf-8") as f:
                return json.load(f).get("entries", {})
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as error:
            logger.error(f"Could not read the credential store: {error}")
            return {}

    def _write_all(self, entries: Dict[str, Any]) -> None:
        os.makedirs(self._dir, mode=0o700, exist_ok=True)
        tmp = f"{self._data_file}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"entries": entries}, f)
        except Exception:
            # Never leave a half-written temp file behind to be picked up later.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, self._data_file)

    # -- api ---------------------------------------------------------------

    def set(self, service: str, **fields: str) -> None:
        """Store fields for a service. Values are encrypted one by one."""
        with self._lock:
            entries = self._read_all()
            entry = entries.setdefault(service, {})
            for name, value in fields.items():
                if value is None:
                    entry.pop(name, None)
                    continue
                entry[name] = self._fernet.encrypt(str(value).encode()).decode()
            self._write_all(entries)

    def get(self, service: str, field: str) -> Optional[str]:
        with self._lock:
            token = self._read_all().get(service, {}).get(field)
        if token is None:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            # Usually the key was regenerated or replaced. Treat it as absent so
            # the user is asked to connect again rather than hitting a traceback.
            logger.warning(
                f"Stored {field} for {service} cannot be decrypted with the current key."
            )
            return None

    def get_all(self, service: str) -> Dict[str, str]:
        with self._lock:
            entry = self._read_all().get(service, {})
        result: Dict[str, str] = {}
        for name in entry:
            value = self.get(service, name)
            if value is not None:
                result[name] = value
        return result

    def has(self, service: str) -> bool:
        with self._lock:
            return bool(self._read_all().get(service))

    def services(self) -> Dict[str, bool]:
        with self._lock:
            return {name: bool(entry) for name, entry in self._read_all().items()}

    def delete(self, service: str) -> None:
        with self._lock:
            entries = self._read_all()
            if entries.pop(service, None) is not None:
                self._write_all(entries)

    def values_to_redact(self) -> list:
        """Every stored plaintext, for the log redaction filter.

        Called on registration and after each save. Decryption failures are
        skipped: a value that cannot be read cannot appear in a log either.
        """
        values = []
        with self._lock:
            entries = self._read_all()
        for service in entries:
            for value in self.get_all(service).values():
                if value and len(value) >= 4:
                    values.append(value)
        return values


def generate_key() -> str:
    """A key for STREAMERBOT_SECRET_KEY, for the menu item that offers one."""
    if Fernet is None:
        raise errors.ServiceError("The cryptography package is not installed.")
    return Fernet.generate_key().decode()


def is_valid_key(key: str) -> bool:
    if Fernet is None or not key:
        return False
    try:
        Fernet(key.strip().encode())
        return True
    except Exception:
        return False


__all__ = ["SecretStore", "generate_key", "is_valid_key", "KEY_ENV_VAR"]
