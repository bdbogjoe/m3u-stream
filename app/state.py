import threading
from typing import Optional

from .m3u import Channel
from .relay import Relay


class AppState:
    def __init__(self, relay_port: int):
        self.lock = threading.Lock()
        self.channels: list[Channel] = []
        self.relay = Relay(relay_port)
        self.current: Optional[Channel] = None
        self.control_url: Optional[str] = None
        self.casting: bool = False

    def channel_by_id(self, cid: str) -> Optional[Channel]:
        for c in self.channels:
            if c.id == cid:
                return c
        return None
