"""Outbound-request politeness: robots.txt compliance and per-host rate limiting.

Every outbound crawl in this project goes through `PoliteCrawler.arun`, which
enforces three things before a request leaves the machine:

1. An identifiable User-Agent carrying a contact address, so an operator who
   sees us in their logs can reach a human.
2. The host's robots.txt rules, evaluated for that User-Agent.
3. A minimum delay between two requests to the same host (1 req/s by default).

robots.txt status handling follows RFC 9309 section 2.3.1:
  - 2xx  -> parse and apply the rules
  - 4xx  -> no restrictions, crawling is allowed
  - 5xx / network error -> treat as "full disallow", we back off rather than
    guess. This is the conservative reading and the one that keeps us out of
    trouble on a flaky host.
"""

import asyncio
import time
from typing import Dict, Optional
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

# Contact address advertised in the User-Agent. An operator who sees this bot
# in their logs can reach a human at this address and ask it to stop.
CONTACT_EMAIL = "kkaced31@gmail.com"
PROJECT_URL = "https://github.com/kromz-dev/leadfinder"

USER_AGENT = f"LeadFinderBot/3.0 (+{PROJECT_URL}; contact: {CONTACT_EMAIL})"

# Minimum seconds between two requests to the same host.
DEFAULT_DELAY = 1.0

# Timeout for the robots.txt fetch itself.
ROBOTS_TIMEOUT = 10.0


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids us from fetching a URL."""


class PoliteCrawler:
    """Wraps a crawl4ai AsyncWebCrawler with robots.txt checks and throttling.

    State is per-host: two different domains are never made to wait on each
    other, but two requests to the same domain always are.
    """

    def __init__(self, delay: float = DEFAULT_DELAY, user_agent: str = USER_AGENT):
        self.delay = delay
        self.user_agent = user_agent
        self._robots: Dict[str, Optional[RobotFileParser]] = {}
        self._last_hit: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _host_key(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def _lock_for(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def _throttle(self, host: str, delay: Optional[float] = None) -> None:
        """Sleep so that at least `delay` seconds elapse between hits on `host`."""
        delay = self.delay if delay is None else delay
        last = self._last_hit.get(host)
        if last is not None:
            waited = time.monotonic() - last
            if waited < delay:
                remaining = delay - waited
                print(f"[rate-limit] {host}: attente {remaining:.2f}s")
                await asyncio.sleep(remaining)
        self._last_hit[host] = time.monotonic()

    def _fetch_robots(self, host: str) -> Optional[RobotFileParser]:
        """Fetch and parse robots.txt. None means 'full disallow'."""
        robots_url = f"{host}/robots.txt"
        try:
            response = requests.get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                timeout=ROBOTS_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            print(f"[robots] {robots_url} injoignable ({exc.__class__.__name__}) -> on s'abstient")
            return None

        if response.status_code >= 500:
            print(f"[robots] {robots_url} a répondu {response.status_code} -> on s'abstient")
            return None

        parser = RobotFileParser()
        parser.set_url(robots_url)
        if 400 <= response.status_code < 500:
            # No robots.txt published: nothing is restricted.
            parser.parse([])
            print(f"[robots] {robots_url}: {response.status_code}, aucune restriction")
        else:
            parser.parse(response.text.splitlines())
            print(f"[robots] {robots_url}: règles chargées")
        return parser

    async def _robots_for(self, host: str) -> Optional[RobotFileParser]:
        if host not in self._robots:
            await self._throttle(host)
            # RobotFileParser + requests are blocking; keep the event loop free.
            self._robots[host] = await asyncio.to_thread(self._fetch_robots, host)
        return self._robots[host]

    # -- public API --------------------------------------------------------

    async def allowed(self, url: str) -> bool:
        """True if robots.txt lets our User-Agent fetch `url`."""
        host = self._host_key(url)
        parser = await self._robots_for(host)
        if parser is None:
            return False
        # Compare on the path only; robotparser wants the full URL but is
        # tolerant, and this keeps query strings intact.
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float:
        """Delay to apply to this host: our floor, or the host's if it asks for more."""
        parser = self._robots.get(self._host_key(url))
        if parser is None:
            return self.delay
        try:
            declared = parser.crawl_delay(self.user_agent)
        except Exception:
            declared = None
        return max(self.delay, float(declared)) if declared else self.delay

    async def arun(self, crawler, url: str, **kwargs):
        """robots.txt-checked, rate-limited call to `crawler.arun`.

        Raises RobotsDisallowed if the host forbids the URL. Any other failure
        is the caller's to handle, as with a bare crawler.arun.
        """
        host = self._host_key(url)
        async with self._lock_for(host):
            if not await self.allowed(url):
                raise RobotsDisallowed(f"robots.txt interdit {url}")
            await self._throttle(host, self.crawl_delay(url))
            return await crawler.arun(url=url, **kwargs)


def normalise(url: str) -> str:
    """Strip fragments so the same page is not fetched twice."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
