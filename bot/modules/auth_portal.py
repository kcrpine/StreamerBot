"""The account-connection web portal.

stdlib ThreadingHTTPServer with a hand-rolled router, matching this repo's
framework-free style. It exists because connecting a streaming account needs a
password field and a 2FA step, neither of which belongs in a TeamTalk chat line
where everyone in the channel can read it.

Access control is deliberately unusual: there is no login page. Adding one would
mean asking a blind user to invent and remember yet another password. Instead
the only way to get a token is a TeamTalk command from a user who already passed
check_access, so authentication happened in TeamTalk and the token is the proof.
A missing or wrong token gets 404 rather than 403, because 403 confirms there is
something here worth attacking.
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from bot.auth import SERVICES, service_name
from bot.auth.session import AuthState
from bot.auth.tokens import TokenStore
from bot.modules.youtube_session_keeper import ImportProblem, MAX_IMPORT_BYTES
from bot.modules import public_address
from bot.modules.portal_pages import PageBuilder

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024

# The session import form carries a cookies.txt file, uploaded or pasted. Real
# ones are a few kilobytes; this leaves room for both fields and multipart
# framing while refusing anything that could only be the wrong file.
MAX_IMPORT_BODY_BYTES = 2 * MAX_IMPORT_BYTES + MAX_BODY_BYTES

# A body bigger than this is not read at all; the connection is closed instead.
MAX_DRAIN_BYTES = 8 * 1024 * 1024

TOO_LARGE = "__too_large__"

# How long "I have approved it" waits for the worker to see Google move on
# before sending the person back to the approval page.
APPROVAL_CHECK_SECONDS = 6


class PortalHandler(BaseHTTPRequestHandler):
    # Set by the server factory below.
    portal: "AuthPortal" = None  # type: ignore[assignment]

    server_version = "StreamerBot"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        # BaseHTTPRequestHandler logs the full request line to stderr, which
        # would put every ?t= token in the log. Route it through logging at
        # debug, with the query string dropped.
        try:
            path = self.path.split("?", 1)[0]
        except Exception:
            path = "?"
        logger.debug(f"[portal] {self.command} {path}")

    # -- helpers -----------------------------------------------------------

    def _send(self, status: int, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        # The portal renders account data and takes passwords; none of it should
        # be cached, framed, or sniffed.
        self.send_header("cache-control", "no-store")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("x-frame-options", "DENY")
        self.send_header(
            "content-security-policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("location", location)
        self.send_header("cache-control", "no-store")
        self.end_headers()

    def _query(self) -> Dict[str, list]:
        return parse_qs(urlparse(self.path).query)

    def _form(self, limit: int = MAX_BODY_BYTES) -> Dict[str, list]:
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > limit:
            # Said as an error rather than silently truncated. Drained so the
            # browser gets the page instead of a connection reset mid-upload.
            if length <= MAX_DRAIN_BYTES:
                self.rfile.read(length)
            else:
                self.close_connection = True
            return {TOO_LARGE: ["1"]}
        body = self.rfile.read(length)
        content_type = self.headers.get("content-type") or ""
        if content_type.lower().startswith("multipart/form-data"):
            return self._multipart(content_type, body)
        return parse_qs(body.decode("utf-8", "replace"))

    @staticmethod
    def _multipart(content_type: str, body: bytes) -> Dict[str, list]:
        """multipart/form-data, for the session import's file field.

        email.parser rather than cgi, which Python 3.13 removed. Every value,
        file or not, comes back as text.
        """
        from email.parser import BytesParser
        from email.policy import default

        blank_line = bytes([13, 10, 13, 10])
        try:
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + content_type.encode("latin-1", "replace") + blank_line + body
            )
        except Exception:
            return {}
        form: Dict[str, list] = {}
        if not message.is_multipart():
            return form
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            form.setdefault(name, []).append(payload.decode("utf-8", "replace"))
        return form

    def _token(self, params: Dict[str, list]) -> str:
        return (params.get("t") or [""])[0]

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        portal = self.portal
        pages = portal.pages
        path = urlparse(self.path).path.rstrip("/") or "/"
        query = self._query()
        limit = MAX_IMPORT_BODY_BYTES if path.startswith("/import") else MAX_BODY_BYTES
        form = self._form(limit) if method == "POST" else {}
        token = self._token(form) or self._token(query)

        if not portal.tokens.check(token):
            # 404 for a token that never existed, 410 for one that expired, with
            # different recovery copy. A bare status page is a dead end for
            # someone who cannot glance at the URL to see the token is missing.
            if token:
                self._send(410, pages.expired_page())
            else:
                self._send(404, pages.not_found_page())
            return

        try:
            self._route(method, path, token, query, form)
        except Exception:
            logger.exception("[portal] Unhandled error")
            self._send(500, pages.server_error_page())

    def _route(
        self,
        method: str,
        path: str,
        token: str,
        query: Dict[str, list],
        form: Dict[str, list],
    ) -> None:
        portal = self.portal
        pages = portal.pages
        parts = [p for p in path.split("/") if p]

        if not parts:
            self._send(200, pages.status_page(
                token, portal.statuses(), youtube_browser=portal.youtube_browser_available()
            ))
            return

        head = parts[0]

        if head == "youtube":
            # The device-code page lived here until Phase 9. Old links still land
            # somewhere useful.
            self._redirect(f"/connect/yt?t={token}")
            return

        if head in ("approve", "import"):
            if len(parts) < 2 or parts[1] != "yt":
                self._send(404, pages.not_found_page())
                return
            getattr(self, f"_route_{head}")(method, token, query, form)
            return

        if head in ("connect", "otp", "disconnect", "progress", "success", "failure"):
            if len(parts) < 2 or parts[1] not in SERVICES:
                self._send(404, pages.not_found_page())
                return
            service = parts[1]
            tail = parts[2] if len(parts) > 2 else ""
            handler = getattr(self, f"_route_{head}")
            handler(method, service, tail, token, form)
            return

        self._send(404, pages.not_found_page())

    # -- routes ------------------------------------------------------------

    def _route_approve(self, method: str, token: str, query: Dict[str, list], form: Dict[str, list]) -> None:
        """Google's "check your phone" step."""
        portal = self.portal
        pages = portal.pages
        job = portal.jobs.get("yt")
        if job is None or job.state is not AuthState.AwaitingApproval:
            self._redirect(f"/progress/yt?t={token}")
            return

        if method == "POST":
            # Nudge the worker to look at once, and give it a moment, so a
            # person who really did approve lands on progress or success rather
            # than straight back here.
            job.confirm_approval()
            deadline = time.monotonic() + APPROVAL_CHECK_SECONDS
            while time.monotonic() < deadline and job.state is AuthState.AwaitingApproval:
                time.sleep(0.25)
            if job.state is AuthState.AwaitingApproval:
                self._redirect(f"/approve/yt?t={token}&not_yet=1")
            else:
                self._redirect(f"/progress/yt?t={token}")
            return

        detail = job.detail
        self._send(200, pages.approval_page(
            token,
            heading=detail.get("approval_heading", ""),
            instruction=detail.get("approval_instruction", ""),
            number=detail.get("approval_number", ""),
            not_yet=bool(query.get("not_yet")),
        ))

    def _route_import(self, method: str, token: str, query: Dict[str, list], form: Dict[str, list]) -> None:
        """A cookies.txt file from the user's own browser."""
        portal = self.portal
        pages = portal.pages
        keeper = portal.youtube_session
        if keeper is None:
            self._send(200, pages.failure_page(
                token, "yt", portal.translator.translate("The YouTube service is not enabled.")
            ))
            return
        browser = keeper.browser_available

        if method == "GET":
            self._send(200, pages.import_page(token, browser_available=browser))
            return

        if form.get(TOO_LARGE):
            problem = ImportProblem.TooLarge
        else:
            # The file wins over the paste box when both are filled: it is the
            # route this page recommends, and cannot have lost its tabs.
            uploaded = (form.get("cookies_file") or [""])[0]
            pasted = (form.get("cookies") or [""])[0]
            text = uploaded if uploaded.strip() else pasted
            ok, problem = keeper.import_text(text)
            if ok:
                self._redirect(f"/success/yt?t={token}")
                return

        message = pages.import_problem_message(problem)
        self._send(200, pages.import_page(token, [("cookies", message)], browser_available=browser))

    def _route_connect(
        self, method: str, service: str, tail: str, token: str, form: Dict[str, list]
    ) -> None:
        portal = self.portal
        pages = portal.pages

        if service == "yt":
            keeper = portal.youtube_session
            if keeper is None:
                self._send(200, pages.failure_page(
                    token, "yt", portal.translator.translate("The YouTube service is not enabled.")
                ))
                return
            if not keeper.browser_available:
                # No Chrome on this host: importing is the only route that works.
                self._redirect(f"/import/yt?t={token}")
                return

        if service == "sp":
            # Device code, not a password form: go-librespot pairs the same way
            # YouTube does.
            if portal.spotify_is_signed_in():
                self._redirect(f"/success/sp?t={token}")
                return
            try:
                info = portal.spotify_start()
            except Exception as error:
                logger.error(f"[portal] Spotify pairing could not start: {error}")
                self._send(200, pages.failure_page(token, "sp", str(error)))
                return
            self._send(
                200,
                pages.device_code_page(
                    token, info.get("user_code", ""), info.get("verification_url", ""),
                    service="sp",
                ),
            )
            return

        if tail == "cancel":
            portal.cancel(service)
            self._redirect(f"/?t={token}")
            return

        if method == "GET":
            self._send(200, pages.credentials_page(token, service))
            return

        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]

        errors = []
        if not username:
            errors.append(
                ("username", pages._("Enter the Google account email address"))
                if service == "yt" else
                ("username", pages._("Enter your %(service)s email address or username")
                 % {"service": service_name(service)})
            )
        if not password:
            errors.append(
                ("password", pages._("Enter the Google account password"))
                if service == "yt" else
                ("password", pages._("Enter your %(service)s password")
                 % {"service": service_name(service)})
            )
        if errors:
            # The username is repopulated, the password never is: SC 3.3.7
            # Redundant Entry, with the password covered by its own exception.
            self._send(200, pages.credentials_page(token, service, username, errors))
            return

        portal.begin_sign_in(service, username, password)
        self._redirect(f"/progress/{service}?t={token}")

    def _route_otp(
        self, method: str, service: str, tail: str, token: str, form: Dict[str, list]
    ) -> None:
        portal = self.portal
        pages = portal.pages
        job = portal.jobs.get(service)

        if tail == "resend" and portal.resend_otp(service):
            self._redirect(f"/otp/{service}?t={token}")
            return

        if job is None or job.state is not AuthState.AwaitingOtp:
            self._redirect(f"/progress/{service}?t={token}")
            return

        username = portal.stored_username(service)
        prompt = job.detail.get("prompt", "")

        if method == "GET":
            self._send(200, pages.otp_page(token, service, username, prompt))
            return

        # Separators and spaces are stripped rather than blocked, so a pasted
        # code with a trailing space is not silently rejected.
        code = "".join((form.get("otp") or [""])[0].split()).replace("-", "")
        if not code:
            self._send(
                200,
                pages.otp_page(
                    token, service, username, prompt,
                    [("otp", pages._("Enter the verification code"))],
                ),
            )
            return

        job.submit_otp(code)
        self._redirect(f"/progress/{service}?t={token}")

    def _route_progress(
        self, method: str, service: str, tail: str, token: str, form: Dict[str, list]
    ) -> None:
        portal = self.portal
        pages = portal.pages
        job = portal.jobs.get(service)

        if job is None:
            self._redirect(f"/?t={token}")
            return
        if job.state is AuthState.AwaitingOtp:
            self._redirect(f"/otp/{service}?t={token}")
            return
        if job.state is AuthState.AwaitingApproval:
            self._redirect(f"/approve/{service}?t={token}")
            return
        if job.state is AuthState.Success:
            self._redirect(f"/success/{service}?t={token}")
            return
        if job.state in (AuthState.Failed, AuthState.AwaitingCaptcha):
            self._redirect(f"/failure/{service}?t={token}")
            return

        self._send(200, pages.progress_page(token, service))

    def _route_success(
        self, method: str, service: str, tail: str, token: str, form: Dict[str, list]
    ) -> None:
        self._send(200, self.portal.pages.success_page(token, service))

    def _route_failure(
        self, method: str, service: str, tail: str, token: str, form: Dict[str, list]
    ) -> None:
        job = self.portal.jobs.get(service)
        reason = job.reason if job else ""
        self._send(200, self.portal.pages.failure_page(token, service, reason))

    def _route_disconnect(
        self, method: str, service: str, tail: str, token: str, form: Dict[str, list]
    ) -> None:
        pages = self.portal.pages
        if method == "GET" or tail == "confirm":
            self._send(200, pages.disconnect_confirm_page(token, service))
            return
        self.portal.disconnect(service)
        self._redirect(f"/?t={token}")


class AuthPortal:
    """Owns the HTTP server, the tokens, and the callbacks into the bot."""

    def __init__(
        self,
        translator,
        store,
        jobs,
        config,
        locale: str = "en",
        youtube_bridge: Optional[Any] = None,
        librespot_engine: Optional[Any] = None,
        sign_in_worker: Optional[Callable[[str, str, str, Any], None]] = None,
        youtube_session: Optional[Any] = None,
    ) -> None:
        self.translator = translator
        self.store = store
        self.jobs = jobs
        self.config = config
        self.pages = PageBuilder(translator, locale)
        self.tokens = TokenStore(getattr(config, "token_ttl", 72000))
        self.youtube_bridge = youtube_bridge
        self.librespot_engine = librespot_engine
        self.sign_in_worker = sign_in_worker
        self.youtube_session = youtube_session
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        handler = type("BoundPortalHandler", (PortalHandler,), {"portal": self})
        self._server = ThreadingHTTPServer(
            (self.config.host, self.config.port), handler
        )
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="AuthPortal", daemon=True
        )
        self._thread.start()
        logger.info(f"Auth portal listening on {self.config.host}:{self.config.port}")
        if self.config.host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning(
                "The auth portal is reachable from outside this machine. "
                "It hands out account connection pages, so keep it behind a firewall."
            )

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.tokens.revoke_all()

    # -- links -------------------------------------------------------------

    def mint_link(self, username: str = "", path: str = "/") -> str:
        """A link the user can actually open.

        This used to hand out http://127.0.0.1:4419, which on a remote Linux box
        is a link to the user's own computer, where nothing is listening. See
        bot/modules/public_address.py for how the address is worked out.
        """
        token = self.tokens.mint(username)
        base, _how = public_address.resolve(self.config)
        return f"{base}{path}?t={token}"

    def link_advice(self) -> Optional[str]:
        """What to change when the link cannot work, or None when it can.

        Handing over an unreachable link with no explanation is the failure this
        replaces: the user sees a browser error and has no way to know the portal
        was listening on loopback.
        """
        base, _how = public_address.resolve(self.config)
        return public_address.reachability_warning(self.config, base)

    def link_source(self) -> str:
        """Which of the sources the address came from, for the ap command."""
        return public_address.resolve(self.config)[1]

    # -- state the pages ask about ----------------------------------------

    def statuses(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for service in SERVICES:
            if service == "yt":
                result[service] = (
                    self.youtube_session.status() if self.youtube_session is not None
                    else "disconnected"
                )
            elif service == "sp":
                result[service] = "connected" if self.spotify_is_signed_in() else "disconnected"
            else:
                result[service] = "connected" if self.store.has(service) else "disconnected"
        return result

    def stored_username(self, service: str) -> str:
        return self.store.get(service, "username") or ""

    def youtube_is_signed_in(self) -> bool:
        return self.youtube_session is not None and self.youtube_session.status() == "connected"

    def youtube_browser_available(self) -> bool:
        return self.youtube_session is not None and self.youtube_session.browser_available

    def spotify_is_signed_in(self) -> bool:
        if self.librespot_engine is None:
            return False
        try:
            return self.librespot_engine.is_signed_in()
        except Exception:
            return False

    def spotify_start(self) -> Dict[str, Any]:
        """The pairing code go-librespot is waiting on.

        Same shape as YouTube's device flow, deliberately: the user reads a code
        and types it at a URL, with no browser redirect and no registered
        developer application anywhere.
        """
        if self.librespot_engine is None:
            raise RuntimeError("The Spotify player is not running.")
        data = self.librespot_engine.auth_code()
        if not data:
            raise RuntimeError("Spotify is already connected, or the player is still starting.")
        return {
            "user_code": data.get("code") or data.get("user_code", ""),
            "verification_url": data.get("url") or data.get("verification_url")
                                or "https://spotify.com/pair",
        }

    # -- actions -----------------------------------------------------------

    def begin_sign_in(self, service: str, username: str, password: str) -> None:
        # Registered immediately so the password cannot reach a log even if the
        # sign-in blows up on the very next line.
        from bot.auth import redaction

        redaction.get_filter().register(password)
        if service != "yt":
            # A Google password is not kept. The session is what the bot keeps,
            # and a dead session needs the person present for the phone prompt
            # anyway, so a stored password could not sign in again on its own.
            self.store.set(service, username=username, password=password)

        job = self.jobs.start(service)
        if self.sign_in_worker is None:
            job.fail(
                self.translator.translate(
                    "Signing in to this service is not available yet."
                )
            )
            return
        threading.Thread(
            target=self.sign_in_worker,
            args=(service, username, password, job),
            name=f"SignIn-{service}",
            daemon=True,
        ).start()

    def resend_otp(self, service: str) -> bool:
        return False

    def cancel(self, service: str) -> None:
        job = self.jobs.get(service)
        if job is not None and not job.is_finished:
            job.fail(self.translator.translate("Cancelled."))
        self.jobs.clear(service)

    def disconnect(self, service: str) -> None:
        if service == "yt" and self.youtube_session is not None:
            try:
                self.youtube_session.sign_out()
            except Exception as error:
                logger.warning(f"[portal] YouTube sign-out failed: {error}")
        if service == "sp" and self.librespot_engine is not None:
            try:
                self.librespot_engine.sign_out()
            except Exception as error:
                logger.warning(f"[portal] Spotify sign-out failed: {error}")
        self.store.delete(service)
        self.jobs.clear(service)


__all__ = ["AuthPortal", "PortalHandler"]
