"""Sign-in jobs and the interactive steps they block on.

A sign-in is not a function call. Netflix and Disney+ ask for a 2FA code partway
through, which means the worker doing the sign-in has to stop and wait for a
human who is somewhere else entirely, on a web page. This models that as a job
with a state, and a queue the worker blocks on while the portal fills it.

    queued -> launching -> filling -> awaiting_otp -> success
                                   -> awaiting_captcha -> failed
                                   -> failed(reason)

Deliberately not a thread per state: the worker is one thread that walks the
sign-in, and the portal thread pokes at this object. Everything mutable is
guarded, because those are genuinely different threads.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from enum import Enum
from queue import Empty, Queue
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# How long the worker waits for a code before giving up. Long enough to find a
# phone, open a message and type; short enough that a forgotten job does not
# hold a browser context open forever.
OTP_TIMEOUT_SECONDS = 300

# A finished job stays visible this long so the portal can render its result.
JOB_RETENTION_SECONDS = 900


class AuthState(Enum):
    Queued = "queued"
    Launching = "launching"
    Filling = "filling"
    AwaitingOtp = "awaiting_otp"
    AwaitingCaptcha = "awaiting_captcha"
    Success = "success"
    Failed = "failed"


TERMINAL_STATES = (AuthState.Success, AuthState.Failed)


class AuthJob:
    """One attempt to connect one service."""

    def __init__(self, service: str) -> None:
        self.id = uuid.uuid4().hex
        self.service = service
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

        self._lock = threading.RLock()
        self._state = AuthState.Queued
        self._reason = ""
        self._detail: Dict[str, Any] = {}

        # Single-slot handoff from the portal thread to the worker thread.
        self._otp_queue: "Queue[str]" = Queue(maxsize=1)

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> AuthState:
        with self._lock:
            return self._state

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def detail(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._detail)

    @property
    def is_finished(self) -> bool:
        return self.state in TERMINAL_STATES

    def set_state(self, state: AuthState, reason: str = "", **detail: Any) -> None:
        with self._lock:
            if self._state in TERMINAL_STATES:
                # A late callback from a worker that already failed must not
                # resurrect the job or overwrite the reason the user was given.
                logger.debug(
                    f"Ignoring {state.value} for {self.service}; job already {self._state.value}"
                )
                return
            self._state = state
            if reason:
                self._reason = reason
            if detail:
                self._detail.update(detail)
            if state in TERMINAL_STATES:
                self.finished_at = time.time()
        logger.debug(f"Auth job {self.service} is now {state.value}")

    def succeed(self) -> None:
        self.set_state(AuthState.Success)

    def fail(self, reason: str) -> None:
        self.set_state(AuthState.Failed, reason=reason)

    # -- the OTP handoff ---------------------------------------------------

    def request_otp(self, prompt: str = "") -> Optional[str]:
        """Called by the worker. Blocks until the portal supplies a code.

        Returns None on timeout, which the caller should treat as a failed
        sign-in rather than retrying: the code will have expired anyway.
        """
        self.set_state(AuthState.AwaitingOtp, prompt=prompt)
        try:
            code = self._otp_queue.get(timeout=OTP_TIMEOUT_SECONDS)
        except Empty:
            self.fail("No verification code was entered in time.")
            return None
        self.set_state(AuthState.Filling)
        return code

    def submit_otp(self, code: str) -> bool:
        """Called by the portal. False if the job was not waiting for a code."""
        with self._lock:
            if self._state is not AuthState.AwaitingOtp:
                return False
        try:
            self._otp_queue.put_nowait(code)
            return True
        except Exception:
            # Already filled by a double submit; the first one wins.
            return False

    def require_captcha(self, url: str = "") -> None:
        """A CAPTCHA appeared. We do not solve these; the user imports a session."""
        self.set_state(
            AuthState.AwaitingCaptcha,
            reason="This service asked for a CAPTCHA, which the bot cannot answer.",
            url=url,
        )


class AuthJobManager:
    """Holds the live jobs, one per service at most."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, AuthJob] = {}

    def start(self, service: str) -> AuthJob:
        """Create a job, replacing any finished one for the same service.

        A second attempt while one is genuinely in flight returns the existing
        job rather than launching a competing browser context.
        """
        with self._lock:
            self._expire()
            existing = self._jobs.get(service)
            if existing is not None and not existing.is_finished:
                return existing
            job = AuthJob(service)
            self._jobs[service] = job
            return job

    def get(self, service: str) -> Optional[AuthJob]:
        with self._lock:
            self._expire()
            return self._jobs.get(service)

    def clear(self, service: str) -> None:
        with self._lock:
            self._jobs.pop(service, None)

    def _expire(self) -> None:
        now = time.time()
        for service, job in list(self._jobs.items()):
            if job.finished_at and now - job.finished_at > JOB_RETENTION_SECONDS:
                del self._jobs[service]


__all__ = [
    "AuthState",
    "AuthJob",
    "AuthJobManager",
    "OTP_TIMEOUT_SECONDS",
    "TERMINAL_STATES",
]
