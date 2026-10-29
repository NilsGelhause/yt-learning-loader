"""Tests for the reverse relay in server.py.

The relay pairs a guest dial-in port (the in-guest agentd sidecar dials OUT
through the slirp NAT) with a master-side client port: bytes are piped both
ways so the master's existing node logic works unchanged over
http://<master-ip>:<clientPort>. These tests bind a real relay pair on
ephemeral bases (39572+i / 39573+i) and drive it with real sockets:

  - byte pipe: a dial-in socket and a client socket exchange bytes
  - 502 fast-fail when no guest has dialed in
  - a second dial-in replaces the first (old channel closed)
  - the guest channel survives a client disconnect (no reconnect storm)
  - start_relays skips (never crashes on) already-bound ports

Run via tests/run.py.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class RelayTests(unittest.TestCase):
    """End-to-end byte plumbing through real relay listener pairs."""

    DIAL_BASE = 39572
    CLIENT_BASE = 39573
    COUNT = 4

    @classmethod
    def setUpClass(cls):
        cls.relays = server.start_relays(dial_base=cls.DIAL_BASE,
                                         client_base=cls.CLIENT_BASE,
                                         count=cls.COUNT)
        if len(cls.relays) != cls.COUNT:
            raise RuntimeError("test relay ports unavailable")
        cls.relay = cls.relays[0]

    @classmethod
    def tearDownClass(cls):
        for relay in cls.relays:
            relay.close()

    def setUp(self):
        # no guest channel may linger from a previous test
        self.clear_guest(self.relay)

    # -- helpers

    @staticmethod
    def clear_guest(relay):
        with relay.lock:
            old, relay.guest = relay.guest, None
        if old is not None:
            try:
                old.close()
            except OSError:
                pass

    @staticmethod
    def wait_guest(relay, timeout=3.0):
        """Poll until the relay has registered a guest channel."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with relay.lock:
                if relay.guest is not None:
                    return relay.guest
            time.sleep(0.02)
        raise AssertionError("guest channel did not register in time")

    @staticmethod
    def recv_exact(conn, n, timeout=5.0):
        conn.settimeout(timeout)
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("peer closed early")
            buf += chunk
        return buf

    # -- the plumbing

    def test_byte_pipe_client_to_guest_and_back(self):
        """A client request reaches the dial-in socket and the dial-in's
        reply reaches the client — full duplex over one relay index."""
        dial = socket.create_connection(("127.0.0.1", self.DIAL_BASE))
        self.addCleanup(dial.close)
        self.wait_guest(self.relay)
        client = socket.create_connection(("127.0.0.1", self.CLIENT_BASE))
        self.addCleanup(client.close)

        request = b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
        client.sendall(request)
        self.assertEqual(self.recv_exact(dial, len(request)), request)

        reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
        dial.sendall(reply)
        self.assertEqual(self.recv_exact(client, len(reply)), reply)

    def test_byte_pipe_with_guest_side_in_a_thread(self):
        """Concurrent exchange: a guest-side echo thread in the background
        while the client round-trips two requests on one connection."""
        dial = socket.create_connection(("127.0.0.1", self.DIAL_BASE))
        self.addCleanup(dial.close)
        self.wait_guest(self.relay)
        stop = threading.Event()

        def echo_guest():
            dial.settimeout(0.2)
            while not stop.is_set():
                try:
                    chunk = dial.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                dial.sendall(chunk)

        thread = threading.Thread(target=echo_guest, daemon=True)
        thread.start()
        self.addCleanup(stop.set)

        client = socket.create_connection(("127.0.0.1", self.CLIENT_BASE))
        self.addCleanup(client.close)
        for payload in (b"ping-1", b"ping-2"):
            client.sendall(payload)
            self.assertEqual(self.recv_exact(client, len(payload)), payload)

    def test_502_fast_fail_when_no_guest(self):
        """With no dial-in, a client gets a tiny 502 and a close — so the
        master's urllib probes fail fast and read the node as offline."""
        relay = self.relays[1]  # index 1 never gets a dial-in here
        self.clear_guest(relay)
        client = socket.create_connection(("127.0.0.1", self.CLIENT_BASE + 1))
        self.addCleanup(client.close)
        head = self.recv_exact(client, len(server._RELAY_OFF))
        self.assertEqual(head, server._RELAY_OFF)
        self.assertTrue(head.startswith(b"HTTP/1.1 502"))
        with self.assertRaises((ConnectionError, socket.timeout)):
            self.recv_exact(client, 1, timeout=2.0)

    def test_second_dial_in_replaces_the_first(self):
        """Whoever dials in last owns the channel; the old socket is closed."""
        first = socket.create_connection(("127.0.0.1", self.DIAL_BASE))
        self.addCleanup(first.close)
        self.assertIsNotNone(self.wait_guest(self.relay))
        first_channel = self.wait_guest(self.relay)

        second = socket.create_connection(("127.0.0.1", self.DIAL_BASE))
        self.addCleanup(second.close)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with self.relay.lock:
                if self.relay.guest is not None and self.relay.guest is not first_channel:
                    break
            time.sleep(0.02)
        else:
            self.fail("second dial-in did not take over the channel")
        # the replaced channel got closed: recv on it returns EOF
        first.settimeout(3.0)
        self.assertEqual(first.recv(1), b"")

        # and the new channel actually pipes
        client = socket.create_connection(("127.0.0.1", self.CLIENT_BASE))
        self.addCleanup(client.close)
        request = b"GET /conversations HTTP/1.1\r\n\r\n"
        client.sendall(request)
        self.assertEqual(self.recv_exact(second, len(request)), request)

    def test_guest_channel_survives_a_client_disconnect(self):
        """A finished HTTP session must NOT tear down the guest's persistent
        dial-out pipe (that would force a reconnect backoff on every
        probe)."""
        dial = socket.create_connection(("127.0.0.1", self.DIAL_BASE))
        self.addCleanup(dial.close)
        channel = self.wait_guest(self.relay)
        client = socket.create_connection(("127.0.0.1", self.CLIENT_BASE))
        client.sendall(b"GET /health HTTP/1.1\r\n\r\n")
        self.recv_exact(dial, len(b"GET /health HTTP/1.1\r\n\r\n"))
        client.close()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with self.relay.lock:
                if self.relay.guest is channel:
                    return  # still registered — the pipe survived
            time.sleep(0.02)
        self.fail("guest channel was dropped by a client disconnect")

    def test_guest_channel_dropped_when_guest_hangs_up(self):
        """When the guest side closes (VM suspended / agentd restarting) the
        channel is cleared so the next client fails fast with 502."""
        dial = socket.create_connection(("127.0.0.1", self.DIAL_BASE))
        channel = self.wait_guest(self.relay)
        client = socket.create_connection(("127.0.0.1", self.CLIENT_BASE))
        client.sendall(b"GET /health HTTP/1.1\r\n\r\n")
        self.recv_exact(dial, len(b"GET /health HTTP/1.1\r\n\r\n"))
        dial.close()  # guest hangs up
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with self.relay.lock:
                if self.relay.guest is not channel:
                    break
            time.sleep(0.02)
        else:
            self.fail("dead guest channel was not dropped")
        client.close()
        probe = socket.create_connection(("127.0.0.1", self.CLIENT_BASE))
        self.addCleanup(probe.close)
        head = self.recv_exact(probe, len(server._RELAY_OFF))
        self.assertTrue(head.startswith(b"HTTP/1.1 502"))

    # -- the API

    def test_relay_index_for_maps_client_ports(self):
        self.assertEqual(server.relay_index_for(18473), 0)
        self.assertEqual(server.relay_index_for(18473 + 5), 5)
        self.assertEqual(server.relay_index_for(18482), 9)

    def test_start_relays_skips_bound_ports(self):
        """A base that is already bound (by a socket WITHOUT the reusable
        address option, like any foreign process) yields fewer relays, not a
        crash — the app server must always come up."""
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # model a foreign process: refuse address sharing entirely
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(("0.0.0.0", 0))  # wildcard: the relay binds 0.0.0.0 too
        blocker.listen(1)
        port = blocker.getsockname()[1]
        again = server.start_relays(dial_base=port, client_base=port + 100,
                                    count=1)
        try:
            self.assertEqual(again, [])
        finally:
            for relay in again:
                relay.close()


if __name__ == "__main__":
    unittest.main()
