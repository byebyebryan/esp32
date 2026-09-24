"""Host-side state model: what the device should be showing right now.

Every change bumps `rev`; `snapshot()` produces a full `sync` message so a
reconnecting or rebooted device can be brought up to date in one message.
"""

from __future__ import annotations

from . import proto


class StateModel:
    def __init__(self, max_visible: int = 3) -> None:
        self.max_visible = max(0, int(max_visible))
        self.rev = 0
        self.clock: dict | None = None
        self.media: dict | None = None
        self.notifs: dict[int, dict] = {}
        self.zones: list[dict] = []

    def snapshot(self) -> dict:
        notifs = list(self.notifs.values())
        overflow = max(0, len(notifs) - self.max_visible)
        if overflow and self.max_visible:
            notifs = notifs[-self.max_visible:]
        elif overflow:
            notifs = []

        snapshot = {
            "t": "sync",
            "rev": self.rev,
            "bar": {"t": "bar", "rev": self.rev, "zones": self.zones},
            "clock": self.clock,
            "media": self.media,
            "notifs": notifs,
            "notifs_overflow": overflow,
        }

        # The device has one fixed 8192-byte input line. Notification strings
        # are individually bounded, but JSON escaping can expand them, so trim
        # the oldest cards until the actual encoded snapshot fits.
        def fits() -> bool:
            try:
                proto.encode(snapshot)
            except ValueError:
                return False
            return True

        while snapshot["notifs"] and not fits():
            snapshot["notifs"].pop(0)
            snapshot["notifs_overflow"] += 1
        if not fits():
            raise ValueError("sync message exceeds the device line limit after notification trimming")
        return snapshot

    def set_clock(self, epoch: int, offset: int) -> bool:
        if self.clock is not None and self.clock["epoch"] == int(epoch) and self.clock["offset"] == int(offset):
            return False
        self.clock = {"epoch": int(epoch), "offset": int(offset)}
        self.rev += 1
        return True

    def set_media(self, message: dict | None) -> bool:
        if message == self.media:
            return False
        self.media = message
        self.rev += 1
        return True

    def set_zones(self, zones: list[dict]) -> bool:
        if zones == self.zones:
            return False
        self.zones = zones
        self.rev += 1
        return True

    def add_notification(self, message: dict) -> bool:
        nid = int(message["id"])
        if self.notifs.get(nid) == message:
            return False
        self.notifs[nid] = message
        self.rev += 1
        return True

    def close_notification(self, nid: int) -> bool:
        nid = int(nid)
        if self.notifs.pop(nid, None) is None:
            return False
        self.rev += 1
        return True
