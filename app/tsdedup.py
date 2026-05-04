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
"""

from __future__ import annotations

import logging
from typing import Iterable, Iterator

log = logging.getLogger("m3u-stream.tsdedup")

TS_PACKET_SIZE = 188
TS_SYNC = 0x47
PTS_HZ = 90000
BACKWARD_TOLERANCE = 60 * PTS_HZ
PTS_MOD = 1 << 33


def _parse_ts(b: bytes) -> int:
    return (
        (((b[0] >> 1) & 0x07) << 30)
        | (b[1] << 22)
        | ((b[2] >> 1) << 15)
        | (b[3] << 7)
        | (b[4] >> 1)
    )


class TsDedup:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._last_dts: dict[int, int] = {}
        self._drop_pid: set[int] = set()
        self._next_cc: dict[int, int] = {}
        self._dropped = 0
        self._emitted = 0

    def filter(self, src: Iterable[bytes]) -> Iterator[bytes]:
        out = bytearray()
        for chunk in src:
            self._buf.extend(chunk)
            while len(self._buf) >= TS_PACKET_SIZE:
                if self._buf[0] != TS_SYNC:
                    i = self._buf.find(bytes((TS_SYNC,)))
                    if i < 0:
                        self._buf.clear()
                        break
                    del self._buf[:i]
                    if len(self._buf) < TS_PACKET_SIZE:
                        break
                pkt = bytearray(self._buf[:TS_PACKET_SIZE])
                del self._buf[:TS_PACKET_SIZE]
                if self._process(pkt):
                    self._rewrite_cc(pkt)
                    out.extend(pkt)
                    self._emitted += 1
                else:
                    self._dropped += 1
            if out:
                yield bytes(out)
                out.clear()

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

    def _process(self, pkt: bytes) -> bool:
        pusi = (pkt[1] >> 6) & 0x01
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        afc = (pkt[3] >> 4) & 0x03

        if pid == 0x1FFF:
            return False  # null packet — strip

        if afc == 0 or afc == 2:
            return pid not in self._drop_pid

        if afc == 3:
            af_len = pkt[4]
            payload_off = 5 + af_len
        else:
            payload_off = 4

        if payload_off >= TS_PACKET_SIZE:
            return pid not in self._drop_pid

        if pusi == 0:
            return pid not in self._drop_pid

        payload = pkt[payload_off:]
        if len(payload) < 14:
            return pid not in self._drop_pid
        if payload[0] != 0 or payload[1] != 0 or payload[2] != 1:
            return pid not in self._drop_pid  # PSI table or padding

        stream_id = payload[3]
        if stream_id in (0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8):
            return pid not in self._drop_pid

        pts_dts_flags = (payload[7] >> 6) & 0x03
        if (pts_dts_flags & 0x02) == 0:
            return pid not in self._drop_pid

        if pts_dts_flags == 0x03:
            if len(payload) < 19:
                return pid not in self._drop_pid
            dts = _parse_ts(payload[14:19])
        else:
            dts = _parse_ts(payload[9:14])

        last = self._last_dts.get(pid)
        if last is None:
            self._last_dts[pid] = dts
            self._drop_pid.discard(pid)
            return True

        diff = dts - last
        if diff > 0 and diff < BACKWARD_TOLERANCE:
            self._last_dts[pid] = dts
            self._drop_pid.discard(pid)
            return True
        if diff <= 0 and -diff < BACKWARD_TOLERANCE:
            self._drop_pid.add(pid)
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
        self._last_dts[pid] = dts
        self._drop_pid.discard(pid)
        return True
