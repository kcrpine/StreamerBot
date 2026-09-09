from enum import Enum


class State(Enum):
    Stopped = "Stopped"
    Playing = "Playing"
    Paused = "Paused"


class Mode(Enum):
    SingleTrack = "st"
    RepeatTrack = "rt"
    TrackList = "tl"
    RepeatTrackList = "rtl"
    Random = "rnd"


class TrackType(Enum):
    Default = 0
    Live = 1
    Local = 2
    Direct = 3
    Dynamic = 4
    # Played by an engine that is not mpv: the Spotify daemon or the browser.
    # There is no stream URL to resolve or hand to a player, so the track's url
    # is a stable identifier (spotify:track:..., netflix://watch/...) and the
    # track is created already "fetched" so lazy resolution never runs.
    External = 5
