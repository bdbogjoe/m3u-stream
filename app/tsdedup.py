"""MPEG-TS DTS-based deduplication.

Filters a byte stream of 188-byte MPEG-TS packets. For each PID, parses the
DTS (or PTS, when DTS is absent) from PES headers and drops any PES whose
timestamp is not strictly increasing — within a 60-second backward tolerance.
Beyond that, treat the jump as a legitimate stream restart and reset state.

DTS, not PTS, is the right signal: with B-frames, PES packets in the stream
are ordered by decode time, so PTS naturally hops backward by a frame or two
within every GOP. DTS is always monotonic.

Continuity counters are rewritten on the way out so that ffmpeg never sees a
discontinuity from dropped packets.

Used in front of ffmpeg when the upstream HTTP server periodically closes the
connection and replies the next request with content slightly earlier in the
stream than where we left off. Byte-level dedup fails when the upstream
re-muxes the TS packets, so DTS-level dedup is the working layer.

The cut lands at an arbitrary byte, usually in the middle of a frame, and the
replay sends that frame again whole. So each PES is held back until the next
one starts on its PID, and at a connection boundary (upstream.BOUNDARY) the
held one is discarded as possibly truncated: the replayed copy then passes the
DTS check instead of being dropped as a duplicate. Passing the truncated frame
on broke every frame referencing it, up to the next keyframe.
"""

from __future__ import annotations

import logging
from typing import Iterable, Iterator, Optional

from .upstream import BOUNDARY

log = logging.getLogger("m3u-stream.tsdedup")

TS_PACKET_SIZE = 188
TS_SYNC = 0x47
PTS_HZ = 90000
BACKWARD_TOLERANCE = 60 * PTS_HZ
PTS_MOD = 1 << 33
NO_TS = -1


def _parse_ts(b: bytes) -> int:
    return (
        (((b[0] >> 1) & 0x07) << 30)
        | (b[1] << 22)
        | ((b[2] >> 1) << 15)
        | (b[3] << 7)
        | (b[4] >> 1)
    )


def _pes_start(pkt: bytes) -> Optional[int]:
    """DTS of the PES starting in this packet, NO_TS if it carries none,
    None if no PES starts here (continuation, PSI table, no payload)."""
    if not (pkt[1] >> 6) & 0x01:
        return None
    afc = (pkt[3] >> 4) & 0x03
    if not (afc & 0x01):
        return None
    payload_off = 5 + pkt[4] if afc == 3 else 4
    if payload_off >= TS_PACKET_SIZE:
        return None
    payload = pkt[payload_off:]
    if len(payload) < 4 or payload[0] != 0 or payload[1] != 0 or payload[2] != 1:
        return None  # PSI table or padding

    stream_id = payload[3]
    if stream_id in (0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8):
        return NO_TS
    if len(payload) < 14:
        return NO_TS
    pts_dts_flags = (payload[7] >> 6) & 0x03
    if (pts_dts_flags & 0x02) == 0:
        return NO_TS
    if pts_dts_flags == 0x03:
        if len(payload) < 19:
            return NO_TS
        return _parse_ts(payload[14:19])
    return _parse_ts(payload[9:14])


class TsDedup:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._last_dts: dict[int, int] = {}     # of the last PES passed on
        self._drop_pid: set[int] = set()        # inside a duplicate PES
        self._await_pusi: set[int] = set()      # after a boundary, until a unit starts
        self._held: dict[int, list[bytearray]] = {}
        self._held_dts: dict[int, int] = {}
        self._next_cc: dict[int, int] = {}
        self._dropped = 0
        self._emitted = 0

    def filter(self, src: Iterable[object]) -> Iterator[bytes]:
        out = bytearray()
        for chunk in src:
            if chunk is BOUNDARY:
                self._boundary()
                continue
            self._buf.extend(chunk)  # type: ignore[arg-type]
            while self._sync():
                pkt = bytearray(self._buf[:TS_PACKET_SIZE])
                del self._buf[:TS_PACKET_SIZE]
                self._process(pkt, out)
            if out:
                yield bytes(out)
                out.clear()
        # The last packet has no successor to confirm its sync byte.
        if len(self._buf) >= TS_PACKET_SIZE and self._buf[0] == TS_SYNC:
            self._process(bytearray(self._buf[:TS_PACKET_SIZE]), out)
        self._buf.clear()
        for pid in list(self._held):
            self._release(pid, out)
        if out:
            yield bytes(out)

    def _sync(self) -> bool:
        """Align the buffer on a packet start; False until a packet is ready.

        A lone 0x47 is common inside payloads, so a start only counts when
        the next packet starts with one too -- which holds the last packet
        back until more bytes arrive.
        """
        while True:
            if len(self._buf) <= TS_PACKET_SIZE:
                return False
            if self._buf[0] == TS_SYNC and self._buf[TS_PACKET_SIZE] == TS_SYNC:
                return True
            i = self._buf.find(bytes((TS_SYNC,)), 1)
            if i < 0:
                self._buf.clear()
                return False
            del self._buf[:i]

    def _boundary(self) -> None:
        """The previous connection ended; whatever of it is unfinished is cut."""
        if self._buf:
            self._dropped += 1
            self._buf.clear()
        for pkts in self._held.values():
            self._dropped += len(pkts)
        self._held.clear()
        self._held_dts.clear()
        # The new connection may open in the middle of a unit; its head is
        # meaningless without the start, which belongs to the dropped tail.
        self._await_pusi.update(self._next_cc)

    def _emit(self, pkt: bytearray, out: bytearray) -> None:
        self._rewrite_cc(pkt)
        out.extend(pkt)
        self._emitted += 1

    def _release(self, pid: int, out: bytearray) -> None:
        pkts = self._held.pop(pid, None)
        if pkts is None:
            return
        dts = self._held_dts.pop(pid)
        if dts != NO_TS:
            self._last_dts[pid] = dts
        for pkt in pkts:
            self._emit(pkt, out)

    def _rewrite_cc(self, pkt: bytearray) -> None:
        afc = (pkt[3] >> 4) & 0x03
        if not (afc & 0x01):
            return  # no payload, CC must not advance
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        next_cc = self._next_cc.get(pid)
        if next_cc is None:
            next_cc = pkt[3] & 0x0F
        pkt[3] = (pkt[3] & 0xF0) | next_cc
        self._next_cc[pid] = (next_cc + 1) & 0x0F

    def _process(self, pkt: bytearray, out: bytearray) -> None:
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid == 0x1FFF:
            self._dropped += 1
            return  # null packet — strip

        if (pkt[1] >> 6) & 0x01:
            self._await_pusi.discard(pid)
        if pid in self._await_pusi:
            self._dropped += 1
            return

        dts = _pes_start(pkt)
        if dts is None:
            if pid in self._drop_pid:
                self._dropped += 1
            elif pid in self._held:
                self._held[pid].append(pkt)
            else:
                self._emit(pkt, out)
            return

        # A new PES: the one held on this PID is now known to be complete.
        self._release(pid, out)
        if dts != NO_TS and not self._accept(pid, dts):
            self._drop_pid.add(pid)
            self._dropped += 1
            return
        self._drop_pid.discard(pid)
        self._held[pid] = [pkt]
        self._held_dts[pid] = dts

    def _accept(self, pid: int, dts: int) -> bool:
        last = self._last_dts.get(pid)
        if last is None:
            return True
        diff = dts - last
        if 0 < diff < BACKWARD_TOLERANCE:
            return True
        if diff <= 0 and -diff < BACKWARD_TOLERANCE:
            if (self._dropped & 0x3F) == 0:
                log.info(
                    "ts-dedup: drop pid=0x%x dts=%d (last=%d, back=%dms, dropped=%d)",
                    pid, dts, last, (-diff * 1000) // PTS_HZ, self._dropped,
                )
            return False
        log.info(
            "ts-dedup: pid=0x%x large jump dts=%d (last=%d, diff=%d), resetting",
            pid, dts, last, diff,
        )
        return True
