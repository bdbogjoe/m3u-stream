import threading
from typing import Optional

from .m3u import Channel
from .relay import Relay


class AppState:
    def __init__(self, relay_port: int, web_url: str = "", web_public_url: str = "",
                 auth_user: str = "", auth_pass: str = ""):
        self.lock = threading.Lock()
        self.channels: list[Channel] = []
        self.relay = Relay(relay_port, web_url=web_url, web_public_url=web_public_url,
                           auth_user=auth_user, auth_pass=auth_pass)
        self.current: Optional[Channel] = None
        self.control_url: Optional[str] = None
        self.casting: bool = False

    def channel_by_id(self, cid: str) -> Optional[Channel]:
        for c in self.channels:
            if c.id == cid:
                return c
        return None
