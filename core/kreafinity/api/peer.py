"""Who is on the other end of a local TCP connection (Linux).

The daemon only accepts connections on 127.0.0.1, but every user of the
machine can reach that address. For a loopback connection the kernel
publishes, in ``/proc/net/tcp``, the user owning the client's socket: the
daemon can thus recognize its own user without any secret — that is how the
web interface is let in, while other users still need the token.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
from pathlib import Path

_TABLES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))

Address = tuple[str, int]


def _decode(field: str) -> Address | None:
    """Decode an ``ADDRESS:PORT`` field of ``/proc/net/tcp[6]``."""
    address_hex, _, port_hex = field.partition(":")
    try:
        port = int(port_hex, 16)
        if len(address_hex) == 8:  # IPv4, one 32-bit word in host byte order
            return socket.inet_ntoa(struct.pack("=I", int(address_hex, 16))), port
        if len(address_hex) == 32:  # IPv6, four 32-bit words in host byte order
            packed = b"".join(
                struct.pack("=I", int(address_hex[i : i + 8], 16)) for i in range(0, 32, 8)
            )
            address = ipaddress.IPv6Address(packed)
            mapped = address.ipv4_mapped
            return (str(mapped) if mapped else str(address)), port
    except ValueError:
        return None
    return None


def peer_uid(client: Address | None, server: Address | None) -> int | None:
    """UID of the process that opened the client side of a local TCP connection.

    Args:
        client: The client's address, as seen by the server.
        server: The server's address of the connection.

    Returns:
        The UID, or None if the connection is not found (not Linux, not
        local, already closed…).
    """
    if not client or not server:
        return None
    try:
        wanted = (str(ipaddress.ip_address(client[0])), client[1])
        remote = (str(ipaddress.ip_address(server[0])), server[1])
    except ValueError:
        return None
    for table in _TABLES:
        try:
            lines = table.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 8:
                continue
            if _decode(fields[1]) == wanted and _decode(fields[2]) == remote:
                try:
                    return int(fields[7])
                except ValueError:
                    return None
    return None
