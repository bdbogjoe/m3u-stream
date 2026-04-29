import logging
import re
import socket
import urllib.request
import xml.etree.ElementTree as ET
from html import escape
from urllib.parse import urljoin

import requests

log = logging.getLogger("m3u-stream.dlna")

UPNP_NS = "urn:schemas-upnp-org:device-1-0"
AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"


class DLNAError(RuntimeError):
    pass


def discover_control_url(tv_ip: str, timeout: float = 4.0) -> str:
    """SSDP M-SEARCH for AVTransport on the LAN.

    Sends both a unicast probe to tv_ip:1900 and a multicast probe to
    239.255.255.250:1900 on the same socket, then keeps the first reply
    coming from tv_ip. Some TVs only answer multicast; sending both
    covers Docker-bridge (multicast not forwarded → unicast wins) and
    host/local networking (TV ignores unicast → multicast wins).
    """
    def _msg(host: str, mx: int) -> bytes:
        return (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {host}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {mx}\r\n"
            f"ST: {AVTRANSPORT}\r\n\r\n"
        ).encode()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    try:
        s.sendto(_msg(f"{tv_ip}:1900", 1), (tv_ip, 1900))
        s.sendto(_msg("239.255.255.250:1900", 2), ("239.255.255.250", 1900))
        while True:
            data, addr = s.recvfrom(4096)
            if addr[0] != tv_ip:
                continue
            m = re.search(r"Location:\s*(\S+)", data.decode(errors="replace"), re.I)
            if not m:
                continue
            desc_url = m.group(1).strip()
            root = ET.fromstring(urllib.request.urlopen(desc_url, timeout=3).read())
            ns = {"u": UPNP_NS}
            for svc in root.iter(f"{{{UPNP_NS}}}service"):
                if svc.findtext("u:serviceType", "", ns).endswith("AVTransport:1"):
                    ctrl = svc.findtext("u:controlURL", "", ns)
                    return urljoin(desc_url, ctrl)
    except socket.timeout:
        pass
    finally:
        s.close()
    raise DLNAError(f"could not discover AVTransport endpoint on {tv_ip}")


def _soap(control_url: str, action: str, body_inner: str) -> requests.Response | None:
    """Returns the response, or None if the TV accepted the request but
    didn't ack within our read timeout (common on webOS while it buffers
    the new stream — the action has actually been performed)."""
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body_inner}</s:Body></s:Envelope>"
    )
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{AVTRANSPORT}#{action}"',
    }
    try:
        return requests.post(control_url, data=envelope, headers=headers,
                             timeout=(5, 30))
    except requests.exceptions.ReadTimeout:
        log.warning("SOAP %s: TV did not ack in time (continuing anyway)", action)
        return None


def set_uri(control_url: str, stream_url: str, title: str) -> None:
    metadata = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="0">'
        f"<dc:title>{escape(title)}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="http-get:*:video/mpeg:*">{escape(stream_url)}</res>'
        "</item></DIDL-Lite>"
    )
    body = (
        f'<u:SetAVTransportURI xmlns:u="{AVTRANSPORT}">'
        "<InstanceID>0</InstanceID>"
        f"<CurrentURI>{escape(stream_url)}</CurrentURI>"
        f"<CurrentURIMetaData>{escape(metadata)}</CurrentURIMetaData>"
        "</u:SetAVTransportURI>"
    )
    r = _soap(control_url, "SetAVTransportURI", body)
    if r is not None and r.status_code >= 400:
        raise DLNAError(f"SetAVTransportURI failed: {r.status_code} {r.text[:800]}")


def play(control_url: str) -> None:
    body = (
        f'<u:Play xmlns:u="{AVTRANSPORT}">'
        "<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>"
    )
    r = _soap(control_url, "Play", body)
    if r is not None and r.status_code >= 400:
        raise DLNAError(f"Play failed: {r.status_code} {r.text[:800]}")


def stop(control_url: str) -> None:
    body = (
        f'<u:Stop xmlns:u="{AVTRANSPORT}">'
        "<InstanceID>0</InstanceID></u:Stop>"
    )
    r = _soap(control_url, "Stop", body)
    if r is not None and r.status_code >= 400:
        raise DLNAError(f"Stop failed: {r.status_code} {r.text[:800]}")
