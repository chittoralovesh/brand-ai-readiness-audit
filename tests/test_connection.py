"""Connection establishment: a dead network path must not cost the whole budget.

`socket.create_connection` walks the resolver's addresses strictly in order and
waits for each to time out. On a machine whose IPv6 path is black-holed - the
norm in containers, CI runners and offices with partial dual-stack - every
request to a dual-stack host therefore pays a full connect timeout before
falling back to IPv4.

Measured on a real dual-stack host with dead IPv6, that was 16 seconds *per
request*: a 20-page audit spent four minutes waiting on a network fault that had
nothing to do with the site being audited, and the robots.txt fetch timed out
first, which made the crawler treat the whole site as disallowed.

The fetcher follows RFC 8305 ("Happy Eyeballs") instead: attempts start a short
delay apart, run concurrently, and the first to complete wins. These tests pin
the ordering policy, the race, and the behaviour when everything fails.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest

from helpers import ROOT  # noqa: F401  (path setup)

from auditlib.fetcher import (CONNECT_STAGGER, Fetcher, _ADDRESS_CACHE,
                              _create_connection, _ordered_addresses)

V4 = socket.AF_INET
V6 = socket.AF_INET6


def info(family, host):
    return (family, socket.SOCK_STREAM, 6, "", (host, 443))


class _Listener:
    """A socket that accepts connections, for racing against a black hole."""

    def __init__(self, host="127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((host, 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except (socket.timeout, OSError):
                continue

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


class TestAddressOrdering(unittest.TestCase):
    def test_families_are_interleaved(self):
        """A run of dead addresses in one family must not delay the other."""
        infos = [info(V6, "::1"), info(V6, "::2"), info(V6, "::3"), info(V4, "10.0.0.1")]
        order = [i[0] for i in _ordered_addresses(infos, None)]
        self.assertEqual(order[:2], [V6, V4],
                         "the second attempt must reach the other family")

    def test_the_known_good_family_goes_first(self):
        infos = [info(V6, "::1"), info(V4, "10.0.0.1")]
        order = [i[0] for i in _ordered_addresses(infos, V4)]
        self.assertEqual(order[0], V4)

    def test_resolver_order_is_preserved_within_a_family(self):
        infos = [info(V4, "10.0.0.1"), info(V4, "10.0.0.2"), info(V4, "10.0.0.3")]
        hosts = [i[4][0] for i in _ordered_addresses(infos, None)]
        self.assertEqual(hosts, ["10.0.0.1", "10.0.0.2", "10.0.0.3"])

    def test_every_address_is_kept_exactly_once(self):
        infos = [info(V6, "::1"), info(V4, "10.0.0.1"), info(V6, "::2")]
        self.assertEqual(sorted(map(str, _ordered_addresses(infos, None))),
                         sorted(map(str, infos)))

    def test_an_unknown_preferred_family_is_ignored(self):
        infos = [info(V4, "10.0.0.1")]
        self.assertEqual(len(_ordered_addresses(infos, V6)), 1)


class TestConnectionRace(unittest.TestCase):
    def setUp(self):
        self.listener = _Listener()
        self.addCleanup(self.listener.close)

    def test_localhost_connects(self):
        sock = _create_connection(("127.0.0.1", self.listener.port), 5.0)
        self.addCleanup(sock.close)
        self.assertIsInstance(sock, socket.socket)

    def test_the_returned_socket_carries_the_callers_timeout(self):
        """Not the race's own timing, or TLS and body reads inherit it."""
        sock = _create_connection(("127.0.0.1", self.listener.port), 4.0)
        self.addCleanup(sock.close)
        self.assertAlmostEqual(sock.gettimeout(), 4.0, places=3)

    def test_a_working_address_is_reached_despite_a_dead_one_listed_first(self):
        """The whole point: a black-holed path must not consume the timeout.

        192.0.2.1 is TEST-NET-1 (RFC 5737) and is guaranteed unroutable, so it
        stands in for the dead IPv6 path. Serial fallback would wait the full
        timeout on it; the race must return well before that.
        """
        real = socket.getaddrinfo

        def fake(host, port, *args, **kwargs):
            if host == "race.test":
                return [info(V4, "192.0.2.1")[:4] + (("192.0.2.1", port),),
                        info(V4, "127.0.0.1")[:4] + (("127.0.0.1", port),)]
            return real(host, port, *args, **kwargs)

        socket.getaddrinfo = fake
        self.addCleanup(setattr, socket, "getaddrinfo", real)
        _ADDRESS_CACHE.forget("race.test")

        started = time.monotonic()
        sock = _create_connection(("race.test", self.listener.port), 8.0)
        elapsed = time.monotonic() - started
        self.addCleanup(sock.close)
        self.assertLess(elapsed, 4.0,
                        "the race must not wait out the unroutable address")
        self.assertGreaterEqual(elapsed, 0.0)

    def test_all_addresses_failing_raises_rather_than_hanging(self):
        started = time.monotonic()
        with self.assertRaises(OSError):
            _create_connection(("192.0.2.1", 443), 1.5)
        self.assertLess(time.monotonic() - started, 6.0,
                        "failure must respect the caller's timeout")

    def test_an_unresolvable_host_raises_a_dns_error(self):
        with self.assertRaises(socket.gaierror):
            _create_connection(("audit-probe-does-not-resolve-9c1f.invalid", 443), 2.0)

    def test_the_stagger_is_short_enough_to_matter(self):
        self.assertLessEqual(CONNECT_STAGGER, 1.0)


class TestFetcherStillWorksOverTheRace(unittest.TestCase):
    def test_a_fixture_site_is_fetched_normally(self):
        from fixture_server import FixtureSite, Route, page
        with FixtureSite({"/": Route(page("Home"))}) as site:
            response = Fetcher(timeout=5.0, rate_per_sec=50.0).fetch(site.url)
        self.assertEqual(response.status, 200)
        self.assertIn("Home", response.body)

    def test_the_read_only_guarantee_is_unaffected(self):
        with self.assertRaises(ValueError):
            Fetcher().fetch("http://example.test/", method="POST")


if __name__ == "__main__":
    unittest.main()
