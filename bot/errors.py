class ParseCommandError(Exception):
    pass


class AccessDeniedError(Exception):
    pass


class UnknownCommandError(Exception):
    pass


class InvalidArgumentError(Exception):
    pass


class ServiceNotFoundError(Exception):
    pass


class ServiceIsDisabledError(Exception):
    pass


class ServiceError(Exception):
    pass


class NothingFoundError(Exception):
    pass


class NoNextTrackError(Exception):
    pass


class NoPreviousTrackError(Exception):
    pass


class IncorrectProtocolError(Exception):
    pass


class PathNotFoundError(Exception):
    pass


class IncorrectTrackIndexError(Exception):
    pass


class NothingIsPlayingError(Exception):
    pass


class IncorrectPositionError(Exception):
    pass


class TTEventError(Exception):
    pass


class ConnectionError(Exception):
    pass


class LoginError(Exception):
    pass


class LocaleNotFoundError(Exception):
    pass


class JoinChannelError(Exception):
    pass


class UnsupportedOperationError(Exception):
    """The active playback engine cannot do this.

    Seeking and speed control exist on mpv but not on every engine: Spotify
    exposes no speed control, and a browser player may refuse a seek while an
    advertisement is on screen. Commands catch this and tell the user the
    operation is not available for the current service, rather than failing.
    """


class EngineUnavailableError(Exception):
    """A playback engine cannot start at all on this host.

    Raised, for example, when the browser engine is asked for on arm64, where
    Google publishes no Chrome and there is therefore no Widevine CDM.
    """


class AuthenticationRequiredError(Exception):
    """The service needs an account connected before it can be used."""


class NotSignedInError(AuthenticationRequiredError):
    """The user has not connected this service yet.

    Carries the service name so the command layer can offer a sign-in link.
    """

    def __init__(self, service: str = "", message: str = "") -> None:
        self.service = service
        super().__init__(message or service)
