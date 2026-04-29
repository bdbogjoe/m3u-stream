import re
import socket
import urllib.request
import xml.etree.ElementTree as ET
from html import escape
from urllib.parse import urljoin

import requests

UPNP_NS = "urn:schemas-upnp-org:device-1-0"
AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"


class DLNAError(RuntimeError):
    pass


def discover_control_url(tv_ip: str, timeout: float = 3.0) -> str:
    """SSDP M-SEARCH for AVTransport, sent unicast to tv_ip:1900.

    Unicast (instead of the usual 239.255.255.250 multicast) so this works
    in both Docker host networking and bridge networking — multicast is not
    forwarded across the bridge by default, but unicast UDP is NAT'd just
    like any other outbound packet.
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {tv_ip}:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        f"ST: {AVTRANSPORT}\r\n\r\n"
    ).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    s.sendto(msg, (tv_ip, 1900))
    try:
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


def _soap(control_url: str, action: str, body_inner: str) -> requests.Response:
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
    return requests.post(control_url, data=envelope, headers=headers, timeout=5)


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
    if r.status_code >= 400:
        raise DLNAError(f"SetAVTransportURI failed: {r.status_code} {r.text[:200]}")


def play(control_url: str) -> None:
    body = (
        f'<u:Play xmlns:u="{AVTRANSPORT}">'
        "<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>"
    )
    r = _soap(control_url, "Play", body)
    if r.status_code >= 400:
        raise DLNAError(f"Play failed: {r.status_code} {r.text[:200]}")


def stop(control_url: str) -> None:
    body = (
        f'<u:Stop xmlns:u="{AVTRANSPORT}">'
        "<InstanceID>0</InstanceID></u:Stop>"
    )
    r = _soap(control_url, "Stop", body)
    if r.status_code >= 400:
        raise DLNAError(f"Stop failed: {r.status_code} {r.text[:200]}")
