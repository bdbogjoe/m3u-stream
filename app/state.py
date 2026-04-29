import threading
from typing import Optional

from .m3u import Channel
from .probe import Prober
from .relay import Relay


class AppState:
    def __init__(self, relay_port: int, web_url: str = "", web_public_url: str = "",
                 auth_user: str = "", auth_pass: str = "", auth_trusted_nets=None):
        self.lock = threading.Lock()
        self.channels: list[Channel] = []
        self.current: Optional[Channel] = None
        self.control_url: Optional[str] = None
        self.casting: bool = False
        self.prober = Prober()

        # Relay resolves channel ids to URLs by asking us back.
        self.relay = Relay(
            relay_port,
            web_url=web_url, web_public_url=web_public_url,
            auth_user=auth_user, auth_pass=auth_pass,
            auth_trusted_nets=auth_trusted_nets or [],
            resolve_url=self._resolve_url,
        )

    def channel_by_id(self, cid: str) -> Optional[Channel]:
        for c in self.channels:
            if c.id == cid:
                return c
        return None

    def _resolve_url(self, cid: str) -> Optional[str]:
        c = self.channel_by_id(cid)
        return c.url if c else None
