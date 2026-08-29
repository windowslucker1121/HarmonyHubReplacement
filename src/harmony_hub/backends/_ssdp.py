"""Shared SSDP (UPnP discovery) plumbing.

More than one backend finds its device the same way: an M-SEARCH multicast,
followed by fetching and skimming the description XML each reply's LOCATION
header points at. What differs between them is only the search target and
which `<manufacturer>` strings count as a match, so this module holds the
wire protocol and each backend keeps its own filter.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Any, List, Sequence

logger = logging.getLogger("HUB.ssdp")

ADDR = "239.255.255.250"
PORT = 1900

#: How long a device is told it may take to answer. The search itself runs
#: for the caller's own `timeout`, spread over two rounds -- this is just the
#: number put in the request.
MX = 2

_LOCATION_HEADER = re.compile(rb"^location:\s*(\S+)", re.IGNORECASE | re.MULTILINE)

#: Matches any of the three description-XML fields a discovery filter cares
#: about. Shared because every backend using this module wants the same
#: three: who made it, and what it calls itself two different ways.
DESC_FIELD = re.compile(r"<(manufacturer|friendlyName|modelName)>([^<]*)</\1>", re.IGNORECASE)


def msearch(search_target: str) -> bytes:
    """One M-SEARCH request. The CRLF line endings and the trailing blank
    line are both mandatory -- a malformed request is not rejected, it is
    silently dropped by every device on the segment, which is a miserable
    thing to debug."""
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {ADDR}:{PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {MX}\r\n"
        f"ST: {search_target}\r\n"
        "\r\n"
    ).encode("ascii")


class _SSDPProtocol(asyncio.DatagramProtocol):
    """Collects the `LOCATION` header out of every reply it receives."""

    def __init__(self) -> None:
        self.locations: set = set()

    def datagram_received(self, data: bytes, addr: Any) -> None:
        match = _LOCATION_HEADER.search(data)
        if match:
            self.locations.add(match.group(1).decode("ascii", "ignore"))

    def error_received(self, exc: Exception) -> None:  # pragma: no cover - defensive
        logger.debug("SSDP socket error: %s", exc)


async def search(timeout: float, targets: Sequence[str]) -> List[str]:
    """Every `LOCATION` URL that answered an M-SEARCH for any of `targets`, deduplicated.

    Its own function, so a test can substitute a backend's own thin wrapper
    around this and exercise discovery without a real network.
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", 0))

    transport, protocol = await loop.create_datagram_endpoint(_SSDPProtocol, sock=sock)
    try:
        # Sent twice, spaced over the window: UDP is lossy on a home network,
        # and a single dropped M-SEARCH is one device silently missing from
        # the list rather than an error anyone would see.
        half = max(timeout, 0.2) / 2
        for _ in range(2):
            for target in targets:
                transport.sendto(msearch(target), (ADDR, PORT))
            await asyncio.sleep(half)
    finally:
        transport.close()
    return sorted(protocol.locations)
