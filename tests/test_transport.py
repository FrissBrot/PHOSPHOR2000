"""Kernel receive timestamps on the cluster UDP socket (real Linux sockets on localhost)."""
import asyncio
import socket
import time

from backend.services.core import UdpTransport, kernel_rx_monotonic


class FakeCore:
    def __init__(self):
        self.got = []
        self.rx = self.rx_rejected = self.tx_errors = 0


async def test_kernel_timestamps_are_monotonic_and_early():
    loop = asyncio.get_running_loop()
    core = FakeCore()
    tr = UdpTransport(core)
    tr.datagram_received = lambda data, addr, stamp: core.got.append((data, stamp, time.monotonic()))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    tr.open(port, loop)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Linux enables RX timestamping system-wide lazily (static key); the very first packets after
    # enabling may be stamped at recvmsg time instead of arrival. In the daemon the socket lives
    # forever, so only the first datagrams after start are affected. Warm up once here.
    tx.sendto(b"warmup", ("127.0.0.1", port))
    await asyncio.sleep(0.05)
    core.got.clear()
    tr.kernel_stamps = 0
    sent = []
    for i in range(5):
        sent.append(time.monotonic())
        tx.sendto(b"x%d" % i, ("127.0.0.1", port))
        time.sleep(0.02)          # block the loop: user-space stamps would be up to 100 ms late
    await asyncio.sleep(0.1)
    loop.remove_reader(tr.sock.fileno())
    assert len(core.got) == 5 and tr.kernel_stamps == 5
    for (data, stamp, handled), t_sent in zip(core.got, sent):
        assert t_sent - 0.002 <= stamp <= t_sent + 0.005      # stamped at arrival
        assert handled - stamp > 0.010 or data == b"x4"        # ... not when Python processed it


def test_kernel_stamp_conversion_rejects_garbage():
    assert kernel_rx_monotonic([], time.time(), time.monotonic()) is None
