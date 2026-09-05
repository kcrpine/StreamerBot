"""HTML for the auth portal.

Built from translated fragments in Python rather than shipped as template files,
so Babel's extractor sees every string.

The markup here follows an accessibility review; several choices look odd until
you know what they are avoiding, so those carry comments. The short version:

- The device code is a **readonly input**, not an aria-hidden paragraph. An
  aria-hidden element is absent from the screen reader's virtual buffer, so a
  blind user could not select or copy the one string they must transcribe.
- Titles carry state, never the code itself: TTS engines mangle "BCDF-GHJK" as
  an attempted word, differently per synth, across seven shipped languages, and
  a title is announced once and is awkward to replay.
- Buttons that navigate are links. Only the disconnect POST is a real button.
- Service names are visible, not hidden inside the control, because a hidden
  span both risks "ConnectNetflix" in the accessible name and hands translators
  a bare verb with no object, which is unbuildable in Turkish and Arabic.
"""

from __future__ import annotations

import html
from typing import Dict, Iterable, List, Optional, Tuple

from bot.auth import service_name

# Locales that render right to left. "ar" already ships.
RTL_LOCALES = {"ar", "he", "fa", "ur"}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def spell_out(code: str, translator) -> str:
    """Render a code for speech: "B, C, D, F, dash, G, H, J, K."

    Comma-space is the reliable cross-AT way to force per-character reading;
    JAWS re-joins space-separated capitals. Symbols become words because a
    literal "-" disappears at most punctuation settings.
    """
    symbols = {
        "-": translator.translate("dash"),
        "_": translator.translate("underscore"),
        ".": translator.translate("dot"),
        " ": translator.translate("space"),
    }
    parts = [symbols.get(ch, ch) for ch in code]
    return ", ".join(parts) + "."


class PageBuilder:
    def __init__(self, translator, locale: str = "en") -> None:
        self.translator = translator
        self.locale = locale or "en"

    def _(self, text: str) -> str:
        return self.translator.translate(text)

    @property
    def direction(self) -> str:
        return "rtl" if self.locale.split("_")[0] in RTL_LOCALES else "ltr"

    def page(self, title: str, body: str, is_error: bool = False) -> str:
        """Wrap body content in the shared shell.

        title is front-loaded state with the app name last. A page rendering
        validation errors is prefixed "Error: " so the state is the first thing
        heard.
        """
        full_title = f"{self._('Error:')} {title}" if is_error else title
        full_title = f"{full_title} - StreamerBot"
        return (
            "<!DOCTYPE html>\n"
            f'<html lang="{esc(self.locale)}" dir="{self.direction}">\n'
            "<head>\n"
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            # The ?t= token is a bearer credential sitting in the URL, and the
            # YouTube page links out to google.com, which would otherwise be
            # handed the token in the Referer header.
            '<meta name="referrer" content="no-referrer">\n'
            f"<title>{esc(full_title)}</title>\n"
            f"<style>{PORTAL_CSS}</style>\n"
            "</head>\n"
            "<body>\n"
            '<header><p class="brand">StreamerBot</p></header>\n'
            f'<main id="main">\n{body}\n</main>\n'
            "</body>\n</html>\n"
        )

    # -- shared fragments --------------------------------------------------

    def error_summary(self, errors: List[Tuple[str, str]]) -> str:
        """errors is [(field_id, message)].

        No role="alert": on a full page load it collides with the focus move
        and the title prefix, double-announcing in JAWS and behaving
        inconsistently in NVDA. The page load, the title and the focus move
        already deliver the message three times.

        Each link targets the input's id, because inputs are natively focusable
        and the fragment jump therefore moves real focus rather than only the
        virtual cursor. The link text must be byte-identical to the field's own
        error text so the user recognises them as the same message.
        """
        if not errors:
            return ""
        items = "\n".join(
            f'<li><a href="#{esc(field)}">{esc(message)}</a></li>'
            for field, message in errors
        )
        return (
            '<div id="error-summary" class="error-summary" tabindex="-1" '
            'aria-labelledby="error-summary-title">\n'
            f'<h2 id="error-summary-title">{esc(self._("There is a problem"))}</h2>\n'
            f'<ul class="error-summary__list">\n{items}\n</ul>\n</div>\n'
            # Focus the summary once, on load. Nothing else on the page takes
            # focus automatically.
            '<script>var s=document.getElementById("error-summary");if(s){s.focus();}</script>\n'
        )

    def field(
        self,
        field_id: str,
        label: str,
        input_html: str,
        hint: str = "",
        error: str = "",
    ) -> str:
        """Label, then hint, then error, then input.

        The error sits before the input in DOM order so it is met in reading
        order and by magnifier users.
        """
        parts = [f'<div class="field{" field--error" if error else ""}">']
        parts.append(f'<label for="{esc(field_id)}">{esc(label)}</label>')
        if hint:
            parts.append(f'<p id="{esc(field_id)}-hint" class="hint">{esc(hint)}</p>')
        if error:
            parts.append(
                f'<p id="{esc(field_id)}-error" class="error-message">'
                f'<span class="visually-hidden">{esc(self._("Error:"))} </span>'
                f"{esc(error)}</p>"
            )
        parts.append(input_html)
        parts.append("</div>")
        return "\n".join(parts)

    def token_field(self, token: str) -> str:
        return f'<input type="hidden" name="t" value="{esc(token)}">'

    def back_link(self, token: str) -> str:
        return (
            f'<p><a href="/?t={esc(token)}">'
            f'{esc(self._("Back to your accounts"))}</a></p>'
        )

    # -- pages -------------------------------------------------------------

    def status_page(self, token: str, statuses: Dict[str, str]) -> str:
        """statuses maps service id to "connected" / "expired" / "disconnected"."""
        connected = sum(1 for s in statuses.values() if s == "connected")
        total = len(statuses)

        rows = []
        for service, state in statuses.items():
            name = service_name(service)
            if state == "connected":
                icon, status_text = "✓", self._("Connected")
                # One verb across all six services and all seven languages:
                # SC 3.2.4 Consistent Identification.
                action = (
                    f'<a class="button" href="/disconnect/{esc(service)}/confirm?t={esc(token)}">'
                    + esc(self._("Disconnect %(service)s") % {"service": name})
                    + "</a>"
                )
            elif state == "expired":
                icon, status_text = "⚠", self._("Expired. Connect again to keep streaming.")
                action = (
                    f'<a class="button" href="/connect/{esc(service)}?t={esc(token)}">'
                    + esc(self._("Connect %(service)s") % {"service": name})
                    + "</a>"
                )
            else:
                icon, status_text = "—", self._("Not connected")
                action = (
                    f'<a class="button" href="/connect/{esc(service)}?t={esc(token)}">'
                    + esc(self._("Connect %(service)s") % {"service": name})
                    + "</a>"
                )
            rows.append(
                '<li class="service">\n'
                # h2, not h3: h1 -> h3 skips a level and breaks the outline the
                # screen reader's element list builds.
                f"<h2>{esc(name)}</h2>\n"
                f'<p class="status status--{esc(state)}">'
                f'<span class="status__icon" aria-hidden="true">{icon}</span> '
                # Status text stands alone without the icon or colour: SC 1.4.1.
                f"{esc(status_text)}</p>\n"
                f'<p class="service__action">{action}</p>\n'
                "</li>"
            )

        body = (
            f'<h1>{esc(self._("Your streaming accounts"))}</h1>\n'
            f'<p>{esc(self._("%(connected)s of %(total)s services connected.") % {"connected": connected, "total": total})}</p>\n'
            f'<ul class="service-list">\n' + "\n".join(rows) + "\n</ul>\n"
        )
        return self.page(
            self._("%(connected)s of %(total)s services connected")
            % {"connected": connected, "total": total},
            body,
        )

    def device_code_page(
        self, token: str, code: str, url: str, expires_text: str = ""
    ) -> str:
        spelled = spell_out(code, self.translator)
        expiry = (
            f'<p id="device-code-expiry">{esc(expires_text)}</p>' if expires_text else ""
        )
        described = "device-code-spelled device-code-expiry" if expires_text else "device-code-spelled"

        body = (
            f'<h1>{esc(self._("Connect YouTube"))}</h1>\n'
            '<ol class="steps">\n'
            f'<li>{esc(self._("Go to"))} '
            f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
            # lang="en" so the synth does not transliterate the hostname in an
            # Arabic or Russian page.
            f'<span lang="en">{esc(url)}</span> '
            f'{esc(self._("(opens in a new tab)"))}</a>.</li>\n'
            f'<li>{esc(self._("Enter the code below."))}</li>\n'
            f'<li>{esc(self._("Come back here and select Check status."))}</li>\n'
            "</ol>\n"
            '<div class="device-code-block">\n'
            f'<label for="device-code">{esc(self._("Your device code"))}</label>\n'
            f'<p id="device-code-spelled" class="visually-hidden">{esc(spelled)}</p>\n'
            # readonly, never disabled: keeps the field focusable and its text
            # selectable, so Ctrl+A / Ctrl+C works and arrow keys spell it out
            # at the user's own verbosity. dir="ltr" keeps the code from being
            # visually reordered inside an RTL page.
            f'<input id="device-code" class="device-code" type="text" '
            f'value="{esc(code)}" readonly size="{max(9, len(code))}" dir="ltr" '
            f'spellcheck="false" autocorrect="off" '
            f'aria-describedby="{described}">\n'
            f"{expiry}\n"
            "</div>\n"
            f'<form method="post" action="/youtube/check?t={esc(token)}">\n'
            f"{self.token_field(token)}\n"
            f'<button type="submit">{esc(self._("Check status"))}</button>\n'
            "</form>\n"
            + self.back_link(token)
        )
        # The code is deliberately not in the title.
        return self.page(self._("Connect YouTube: enter your code"), body)

    def credentials_page(
        self,
        token: str,
        service: str,
        username: str = "",
        errors: Optional[List[Tuple[str, str]]] = None,
    ) -> str:
        errors = errors or []
        name = service_name(service)
        error_for = dict(errors)

        username_input = (
            f'<input id="username" name="username" type="text" '
            f'value="{esc(username)}" autocomplete="username" autocapitalize="none" '
            f'autocorrect="off" spellcheck="false" required'
            + (' aria-invalid="true"' if "username" in error_for else "")
            + f' aria-describedby="{"username-error " if "username" in error_for else ""}username-hint">'
        )
        # Never repopulated, and never given a maxlength.
        password_input = (
            '<input id="password" name="password" type="password" '
            'autocomplete="current-password" required'
            + (' aria-invalid="true"' if "password" in error_for else "")
            + (' aria-describedby="password-error"' if "password" in error_for else "")
            + ">"
        )

        body = (
            f'<h1>{esc(self._("Sign in to %(service)s") % {"service": name})}</h1>\n'
            + self.error_summary(errors)
            # novalidate: server-rendered errors are the source of truth. required
            # stays, because it still maps to aria-required.
            + f'<form method="post" action="/connect/{esc(service)}?t={esc(token)}" novalidate>\n'
            + self.token_field(token)
            + "\n"
            + self.field(
                "username",
                self._("%(service)s email address or username") % {"service": name},
                username_input,
                hint=self._("The address you use to sign in to %(service)s.") % {"service": name},
                error=error_for.get("username", ""),
            )
            + "\n"
            + self.field(
                "password",
                self._("%(service)s password") % {"service": name},
                password_input,
                error=error_for.get("password", ""),
            )
            + "\n"
            f'<button type="submit">{esc(self._("Sign in to %(service)s") % {"service": name})}</button>\n'
            "</form>\n"
            f'<p><a href="/?t={esc(token)}">{esc(self._("Cancel and go back"))}</a></p>'
        )
        return self.page(
            self._("Sign in to %(service)s") % {"service": name}, body, is_error=bool(errors)
        )

    def otp_page(
        self,
        token: str,
        service: str,
        username: str = "",
        prompt: str = "",
        errors: Optional[List[Tuple[str, str]]] = None,
    ) -> str:
        errors = errors or []
        name = service_name(service)
        error_for = dict(errors)

        # type="text" with inputmode="numeric", never type="number": a
        # spinbutton announces as one, arrow keys change the value instead of
        # moving the caret, and leading zeros are dropped. No maxlength: a
        # pasted code with a trailing space would be silently truncated.
        otp_input = (
            '<input id="otp" name="otp" type="text" inputmode="numeric" '
            'autocomplete="one-time-code" autocapitalize="none" autocorrect="off" '
            'spellcheck="false" dir="ltr" required'
            + (' aria-invalid="true"' if "otp" in error_for else "")
            + f' aria-describedby="{"otp-error " if "otp" in error_for else ""}otp-hint">'
        )

        # Showing the username satisfies SC 3.3.7 Redundant Entry: the user
        # does not retype what they already gave us.
        signing_in = (
            f'<p>{esc(self._("Signing in as %(username)s.") % {"username": username})}</p>\n'
            if username
            else ""
        )

        body = (
            f'<h1>{esc(self._("Enter your %(service)s verification code") % {"service": name})}</h1>\n'
            + self.error_summary(errors)
            + signing_in
            + f'<form method="post" action="/otp/{esc(service)}?t={esc(token)}" novalidate>\n'
            + self.token_field(token)
            + "\n"
            + self.field(
                "otp",
                self._("Verification code"),
                otp_input,
                hint=prompt
                or self._(
                    "Type or paste the whole code into the box below. Spaces do not matter."
                ),
                error=error_for.get("otp", ""),
            )
            + "\n"
            f'<button type="submit">{esc(self._("Verify code"))}</button>\n'
            "</form>\n"
            f'<p><a href="/otp/{esc(service)}/resend?t={esc(token)}">'
            f'{esc(self._("Send a new code"))}</a></p>\n'
            f'<p><a href="/connect/{esc(service)}/cancel?t={esc(token)}">'
            f'{esc(self._("Cancel this sign-in"))}</a></p>'
        )
        return self.page(
            self._("Enter your %(service)s verification code") % {"service": name},
            body,
            is_error=bool(errors),
        )

    def progress_page(self, token: str, service: str, message: str = "") -> str:
        name = service_name(service)
        body = (
            f'<h1>{esc(self._("Signing in to %(service)s") % {"service": name})}</h1>\n'
            f'<p>{esc(message or self._("Waiting for the service to confirm. This usually takes 10 to 30 seconds."))}</p>\n'
            # Present and empty in the initial HTML: a live region injected at
            # the same moment as its text is not reliably announced. role=status
            # already implies polite and atomic, so those are not repeated.
            '<p id="poll-status" role="status"></p>\n'
            f'<p><a href="/progress/{esc(service)}?t={esc(token)}">'
            f'{esc(self._("Check again"))}</a></p>\n'
            f'<p><a href="/connect/{esc(service)}/cancel?t={esc(token)}">'
            f'{esc(self._("Cancel this sign-in"))}</a></p>'
        )
        return self.page(
            self._("Signing in to %(service)s") % {"service": name}, body
        )

    def success_page(self, token: str, service: str) -> str:
        name = service_name(service)
        body = (
            f'<h1>{esc(self._("%(service)s connected") % {"service": name})}</h1>\n'
            f'<p>{esc(self._("StreamerBot can now stream from your %(service)s account.") % {"service": name})}</p>\n'
            + self.back_link(token)
        )
        return self.page(self._("%(service)s connected") % {"service": name}, body)

    def failure_page(self, token: str, service: str, reason: str = "") -> str:
        name = service_name(service)
        body = (
            f'<h1>{esc(self._("%(service)s could not be connected") % {"service": name})}</h1>\n'
            f'<p>{esc(reason or self._("The sign-in did not complete."))}</p>\n'
            f'<p><a href="/connect/{esc(service)}?t={esc(token)}">'
            f'{esc(self._("Try again"))}</a></p>\n'
            + self.back_link(token)
        )
        return self.page(
            self._("%(service)s could not be connected") % {"service": name}, body
        )

    def disconnect_confirm_page(self, token: str, service: str) -> str:
        name = service_name(service)
        body = (
            f'<h1>{esc(self._("Disconnect %(service)s?") % {"service": name})}</h1>\n'
            f'<p>{esc(self._("StreamerBot will stop streaming from your %(service)s account. Your %(service)s account itself is not affected. You can connect again at any time.") % {"service": name})}</p>\n'
            # POST, not a GET link: a link that deletes data gets fired by
            # prefetchers, link scanners and antivirus proxies. This page is
            # also what satisfies SC 3.3.4 Error Prevention.
            f'<form method="post" action="/disconnect/{esc(service)}?t={esc(token)}">\n'
            + self.token_field(token)
            + "\n"
            f'<button type="submit" class="button--destructive">'
            f'{esc(self._("Disconnect %(service)s") % {"service": name})}</button>\n'
            "</form>\n"
            f'<p><a href="/?t={esc(token)}">'
            f'{esc(self._("Cancel, keep %(service)s connected") % {"service": name})}</a></p>'
        )
        return self.page(
            self._("Disconnect %(service)s?") % {"service": name}, body
        )

    def message_page(self, title: str, message: str, recovery: str = "") -> str:
        """Used for every terminal state: 404, 410, 500, rate limit, timeout.

        A bare status page is a dead end for a blind user, who cannot glance at
        the URL to work out that a token is missing, so every one of these
        carries an explicit next step.
        """
        recovery_html = f"<p>{esc(recovery)}</p>\n" if recovery else ""
        body = f"<h1>{esc(title)}</h1>\n<p>{esc(message)}</p>\n{recovery_html}"
        return self.page(title, body)

    def not_found_page(self) -> str:
        return self.message_page(
            self._("Page not found"),
            self._("This address is not quite right."),
            self._("To get a new link, send the li command to StreamerBot in TeamTalk."),
        )

    def expired_page(self) -> str:
        return self.message_page(
            self._("Link expired"),
            self._("This link has expired."),
            self._("To get a new link, send the li command to StreamerBot in TeamTalk."),
        )

    def server_error_page(self) -> str:
        return self.message_page(
            self._("Something went wrong"),
            self._("StreamerBot could not finish that request."),
            self._("Try again in a moment. If it keeps happening, check the bot's log."),
        )


PORTAL_CSS = """
:root{--text:#1b1b1b;--bg:#fff;--link:#0842a0;--focus:#0842a0;--focus-inner:#fff;
--error:#8c1d18;--muted:#5a5a5a}
body{color:var(--text);background:var(--bg);font-size:1rem;line-height:1.5;
font-family:system-ui,sans-serif;max-inline-size:70ch;margin-inline:auto;padding-inline:1rem}
a{color:var(--link);text-decoration:underline;text-underline-offset:.2em}
h1{font-size:1.6rem;margin-block:1rem .5rem}
h2{font-size:1.2rem;margin-block:1rem .25rem}
.brand{color:var(--muted);font-weight:700;margin-block:1rem 0}
/* clip-path, never display:none or width:0, all of which drop the node from
   the accessibility tree. white-space stops per-character wrapping. */
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;
border:0;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
/* SC 2.4.13 Focus Appearance: 3px at 2px offset clears the 2px-perimeter area,
   and #0842a0 on white is 8.6:1, well past the 3:1 floor. Never outline:none. */
:focus-visible{outline:3px solid var(--focus);outline-offset:2px;
box-shadow:0 0 0 2px var(--focus-inner);border-radius:2px}
/* :focus-visible does not reliably match a programmatically focused
   tabindex="-1" element, so the summary uses plain :focus. */
.error-summary:focus{outline:3px solid var(--focus);outline-offset:2px}
.error-summary{border:4px solid var(--error);padding:.75rem 1rem;margin-block:1rem}
.error-summary h2{margin-block-start:0}
.error-message{color:var(--error);font-weight:700;margin-block:.25rem}
.field{margin-block:1.25rem}
.field label{display:block;font-weight:700;margin-block-end:.25rem}
.hint{color:var(--muted);margin-block:.25rem}
input[type=text],input[type=password]{font-size:1rem;padding:.5rem;
inline-size:100%;max-inline-size:24rem;box-sizing:border-box}
.device-code{font-family:monospace;font-size:1.5rem;letter-spacing:.15em}
/* SC 2.5.8 Target Size: 44px comfortably clears the 24px minimum. */
.button,button{display:inline-block;min-block-size:44px;min-inline-size:44px;
padding:.625rem 1rem;font-size:1rem;line-height:1.5;cursor:pointer}
.button--destructive{border:2px solid var(--error);color:var(--error);background:#fff}
.service-list{list-style:none;padding-inline-start:0}
.service{border-block-end:1px solid #d0d0d0;padding-block:.75rem}
.service__action{margin-block:.75rem}
.status{margin-block:.25rem}
/* High contrast drops box-shadow and background images, so anything
   load-bearing is an outline or a border here. */
@media (forced-colors:active){
:focus-visible,.error-summary:focus{outline:3px solid Highlight;box-shadow:none}
.error-summary{border:2px solid CanvasText}
.button,button{border:1px solid ButtonText}}
@media (prefers-reduced-motion:reduce){
*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
transition-duration:.01ms!important;scroll-behavior:auto!important}}
@media (prefers-color-scheme:dark){
:root{--text:#e8e8e8;--bg:#161616;--link:#8ab4f8;--focus:#8ab4f8;--focus-inner:#161616;
--error:#f2b8b5;--muted:#b0b0b0}
.button--destructive{background:#161616}
.service{border-block-end-color:#3a3a3a}}
""".strip()


__all__ = ["PageBuilder", "spell_out", "esc", "PORTAL_CSS"]
