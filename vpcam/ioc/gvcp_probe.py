"""
gvcp_probe.py — dependency-free GigE Vision discovery probe (no root needed).

Sends a GVCP DISCOVERY_CMD as a subnet broadcast and (optionally) unicast to
a specific camera IP, then prints every reply with vendor/model/serial.

    python gvcp_probe.py LOCAL_IP [CAMERA_IP]
    python gvcp_probe.py 192.168.136.2 192.168.136.23

Interpreting results:
    replies to broadcast AND unicast -> network path is fine; the problem is
                                        in the GenTL producer / harvesters layer
    unicast reply only               -> broadcasts are being filtered
                                        (switch isolation etc.) — the producer
                                        relies on broadcast, so that's the bug
    no replies at all                -> camera unreachable from this interface
                                        (host firewall, camera off/held, or
                                        wrong subnet after all)

Assumes a /24 for the subnet-broadcast address.
"""

import socket
import struct
import sys
import time


def _cstr(b: bytes) -> str:
    return b.split(b"\0")[0].decode("ascii", "replace").strip()


def _parse(data: bytes, addr) -> str:
    if len(data) < 8:
        return f"{addr[0]}: short packet ({len(data)} bytes)"
    _status, answer, _length, _ackid = struct.unpack(">HHHH", data[:8])
    if answer != 0x0003:  # DISCOVERY_ACK
        return f"{addr[0]}: unexpected answer 0x{answer:04x}"
    p = data[8:]
    # Discovery ack payload mirrors the GVBS bootstrap registers
    ip = ".".join(str(x) for x in p[36:40]) if len(p) >= 40 else "?"
    vendor = _cstr(p[72:104]) if len(p) >= 104 else "?"
    model = _cstr(p[104:136]) if len(p) >= 136 else "?"
    serial = _cstr(p[216:232]) if len(p) >= 232 else "?"
    return f"{addr[0]}: {vendor} {model} (serial {serial}, reports IP {ip})"


def probe(local_ip: str, dest: str, label: str):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind((local_ip, 0))   # source IP picks the outgoing interface
    s.settimeout(0.5)
    # GVCP header: key 0x42, flags 0x11 (ack required | allow broadcast ack),
    # command DISCOVERY_CMD (0x0002), payload length 0, request id 0xffff
    s.sendto(bytes([0x42, 0x11, 0x00, 0x02, 0x00, 0x00, 0xFF, 0xFF]),
             (dest, 3956))
    replies = []
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            data, addr = s.recvfrom(2048)
            replies.append(_parse(data, addr))
        except socket.timeout:
            continue
        except OSError:
            # Windows surfaces ICMP port-unreachable as ConnectionResetError
            continue
    s.close()
    print(f"[{label}] -> {dest}:3956 : {len(replies)} repl"
          f"{'y' if len(replies) == 1 else 'ies'}")
    for r in replies:
        print(f"    {r}")
    return replies


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    local_ip = sys.argv[1]
    bcast = ".".join(local_ip.split(".")[:3] + ["255"])
    probe(local_ip, bcast, "subnet broadcast")
    if len(sys.argv) > 2:
        probe(local_ip, sys.argv[2], "unicast")
