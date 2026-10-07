"""A declared Crawl-delay is honoured in full, by sampling less.

Found live: Hacker News declares `Crawl-delay: 30`. The limiter clamped that to
10 seconds and then capped each individual sleep at 5, so a site asking for one
request every 30 seconds received roughly one every 5. The audit's own safety
rule is that robots.txt is respected *without exception*, and a Crawl-delay is
part of robots.txt.

Inside a 240-second budget a 30-second delay leaves two options: fetch fewer
pages, or fetch faster than permitted. This module pins the first. The reduced
sample is recorded in the crawl notes, so every ratio in the report still states
the denominator it was actually measured against.

The sizing decision is exercised directly rather than over the wire: a test that
waited out the delay it is verifying would take minutes to assert arithmetic.
Two end-to-end cases with a short delay cover the wiring.
"""

from __future__ import annotations

import time
import unittest

from fixture_server import FixtureSite, RICH_PARAGRAPH, Route, page
from helpers import crawl

from auditlib.budget import Budget
from auditlib.crawler import MAX_PAGES, Crawl
from auditlib.fetcher import MAX_HONOURED_CRAWL_DELAY, Fetcher, _HostLimiter


def sized(delay, total=240.0):
    """A crawl whose page target has been fitted to `delay`, without fetching."""
    session = Crawl("https://example.test/", budget=Budget(total=total),
                    fetcher=Fetcher(rate_per_sec=50.0))
    session.crawl_delay = float(delay)
    session._fit_sample_to_crawl_delay()
    return session


def site_with_delay(delay, pages=5):
    links = "".join('<a href="/p%d">Section %d</a>' % (i, i) for i in range(pages))
    robots = ("User-agent: *\nAllow: /\n" if delay is None
              else "User-agent: *\nCrawl-delay: %s\nAllow: /\n" % delay)
    routes = {"/robots.txt": Route(robots, content_type="text/plain"),
              "/": Route(page("Home", body=RICH_PARAGRAPH + links))}
    for i in range(pages):
        routes["/p%d" % i] = Route(page("Section %d" % i, body=RICH_PARAGRAPH))
    return routes


class TestTheLimiterWaitsInFull(unittest.TestCase):
    def test_a_long_interval_is_not_silently_capped(self):
        """The old code slept `min(wait, 5.0)`, which is not the interval asked for."""
        limiter = _HostLimiter(rate=100.0, concurrency=1)
        limiter.slow_down(0.4)
        limiter.acquire(); limiter.release()          # the first slot is free
        started = time.monotonic()
        limiter.acquire(); limiter.release()
        self.assertGreaterEqual(time.monotonic() - started, 0.35,
                                "the limiter must wait the full declared interval")

    def test_slow_down_never_speeds_the_limiter_up(self):
        limiter = _HostLimiter(rate=0.5, concurrency=1)   # already a 2s interval
        limiter.slow_down(0.1)
        self.assertGreaterEqual(limiter.min_interval, 2.0)

    def test_an_absurd_delay_is_bounded_but_still_large(self):
        limiter = _HostLimiter(rate=100.0, concurrency=1)
        limiter.slow_down(100000.0)
        self.assertEqual(limiter.min_interval, MAX_HONOURED_CRAWL_DELAY)


class TestSampleFitsTheDeclaredDelay(unittest.TestCase):
    def test_a_thirty_second_delay_shrinks_the_sample(self):
        session = sized(30)
        self.assertLess(session.max_pages, MAX_PAGES)
        self.assertGreaterEqual(session.max_pages, 1)

    def test_the_reduction_is_recorded_with_its_reason(self):
        notes = " ".join(sized(30).notes)
        self.assertIn("Crawl-delay", notes)
        self.assertIn("honoured in full", notes)

    def test_a_small_delay_leaves_the_sample_alone(self):
        self.assertEqual(sized(1).max_pages, MAX_PAGES)

    def test_no_declared_delay_leaves_the_sample_alone(self):
        self.assertEqual(sized(0).max_pages, MAX_PAGES)

    def test_the_sample_never_drops_below_one_page(self):
        """An extreme delay still audits the entry page rather than nothing."""
        self.assertGreaterEqual(sized(100000).max_pages, 1)

    def test_a_longer_delay_yields_a_smaller_sample(self):
        self.assertGreater(sized(10).max_pages, sized(30).max_pages)

    def test_the_sample_never_exceeds_what_the_delay_affords(self):
        for delay in (10, 20, 30, 60):
            with self.subTest(delay=delay):
                session = sized(delay)
                effective = min(delay, MAX_HONOURED_CRAWL_DELAY)
                self.assertLessEqual(session.max_pages * effective,
                                     session.budget.total,
                                     "the sample must fit inside the budget at the "
                                     "declared pace")

    def test_the_adaptive_expansion_cannot_undo_the_reduction(self):
        """The headroom widening must not re-widen a sample the site shrank."""
        session = sized(30)
        reduced = session.max_pages
        session._maybe_expand_capacity()
        self.assertLess(reduced, MAX_PAGES)
        self.assertEqual(session.max_pages, reduced)

    def test_the_expansion_prices_pages_at_the_declared_delay(self):
        """A budget large enough to expand must still plan at the site's pace.

        Sizing the expansion at the default per-page estimate would plan a
        sample the declared delay cannot deliver inside the deadline.
        """
        session = sized(30, total=3000.0)
        session._maybe_expand_capacity()
        self.assertLessEqual(session.max_pages * 30, 3000.0,
                             "the expanded sample must be deliverable at 30s/page")

    def test_expansion_is_unaffected_when_no_delay_is_declared(self):
        session = sized(0, total=3000.0)
        session._maybe_expand_capacity()
        self.assertGreater(session.max_pages, MAX_PAGES)


class TestEndToEnd(unittest.TestCase):
    def test_the_declared_pace_is_actually_kept(self):
        """Requests must be at least the declared interval apart."""
        with FixtureSite(site_with_delay(1)) as site:
            started = time.monotonic()
            session = crawl(site.url, budget=Budget(total=240.0), rate=50.0)
            elapsed = time.monotonic() - started
        fetched = len([p for p in session.snapshot()["pages"] if p.get("ok")])
        self.assertGreaterEqual(fetched, 2)
        self.assertGreaterEqual(elapsed, (fetched - 1) * 1.0,
                                "a 1s Crawl-delay means 1s between requests")

    def test_the_note_reaches_the_snapshot(self):
        with FixtureSite(site_with_delay(1)) as site:
            snapshot = crawl(site.url, budget=Budget(total=240.0), rate=50.0).snapshot()
        self.assertTrue(any("Crawl-delay" in n for n in snapshot["notes"]))
        self.assertTrue(snapshot["pages"], "a delayed crawl still produces a sample")


if __name__ == "__main__":
    unittest.main()
