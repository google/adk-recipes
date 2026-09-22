# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Defines Computer Use browser controller and toolset for visual search navigation."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import urllib.parse
from typing import Any, Literal
from urllib.parse import urlparse

from google.adk.tools.computer_use.base_computer import (
    BaseComputer,
    ComputerEnvironment,
    ComputerState,
)
from google.adk.tools.computer_use.computer_use_toolset import (
    ComputerUseToolset,
)

from ..shared_libraries import constants

logger = logging.getLogger(__name__)

# Default standard viewport resolution for retail browser search
DEFAULT_SCREEN_SIZE: tuple[int, int] = (1280, 800)
DEFAULT_TIMEOUT_MS: int = 5000
DEFAULT_SCROLL_OFFSET: int = 500
MAX_WAIT_SECONDS: int = 5
POST_SCROLL_WAIT_SECONDS: float = 0.3
GOOGLE_SHOPPING_SEARCH_URL: str = "https://www.google.com/search?tbm=shop"
DEFAULT_INITIAL_SEARCH_URL: str = (
    f"{GOOGLE_SHOPPING_SEARCH_URL}&q=running+shoes"
)

_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_DISALLOWED_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "metadata.google.internal", "instance-data"}
)
# Hostnames are restricted to the characters a browser accepts verbatim.
# Anything else -- in particular non-ASCII labels, which Chromium IDNA-encodes
# and urllib.parse does not -- is rejected rather than guessed at.
_SAFE_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9.\-:\[\]]+$")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reports whether an address belongs to a range the agent must not reach."""
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _has_parser_confusion_chars(url: str) -> bool:
    """Reports characters that Chromium and urllib.parse disagree about.

    Chromium strips ASCII control characters (notably tab, CR and LF) anywhere
    in a URL before parsing it, and treats a backslash as a separator inside the
    authority. urllib.parse does neither, so a URL containing any of them can
    designate one host here and a different one in the browser. Such URLs are
    rejected rather than normalised, since normalisation would mean
    reimplementing the WHATWG URL parser.
    """
    return "\\" in url or any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in url
    )


def _normalize_hostname(hostname: str) -> str:
    """Lower-cases a hostname and drops the explicit DNS root label.

    A trailing dot marks the root of a fully qualified name and does not change
    what the name resolves to, so "metadata.google.internal." reaches the same
    host as "metadata.google.internal" while matching neither _DISALLOWED_HOSTS
    nor the blocked suffixes. Removing it keeps the literal checks from being
    sidestepped that way. The same applies to IP literals, where "127.0.0.1."
    would otherwise fail to parse as an address and fall through the check.
    """
    return hostname.lower().rstrip(".")


def _validate_navigation_url(url: str) -> bool:
    """Validates navigation URLs to prevent SSRF against internal/metadata endpoints.

    Only inspects the URL itself. Callers that are about to fetch the URL should
    use :func:`validate_navigation_target`, which also resolves the hostname.
    """
    try:
        if _has_parser_confusion_chars(url):
            return False
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
            return False
        if parsed.username or parsed.password:
            # "https://www.google.com@metadata.google.internal/" reads as a
            # trusted host but resolves to the userinfo-suffixed one.
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        hostname_lower = _normalize_hostname(hostname)
        if not hostname_lower:
            return False
        if not _SAFE_HOSTNAME_PATTERN.match(hostname_lower):
            return False
        if hostname_lower in _DISALLOWED_HOSTS or hostname_lower.endswith(
            (".internal", ".local")
        ):
            return False
        try:
            ip = ipaddress.ip_address(hostname_lower)
            if _is_blocked_ip(ip):
                return False
        except ValueError:
            # Not an IP literal, valid public hostname format
            pass
        return True
    except Exception:
        return False


def _resolved_addresses_allowed(hostname: str) -> bool:
    """Resolves a hostname and rejects it if any address is private or local.

    A public-looking hostname can still resolve to a loopback, private or
    metadata address, so the literal checks in :func:`_validate_navigation_url`
    are not sufficient on their own.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Unresolvable host: let the browser surface the failure itself.
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            return False
    return True


async def validate_navigation_target(url: str) -> bool:
    """Validates a URL and the addresses its hostname resolves to.

    The resolution check is best-effort defence in depth, not a security
    boundary: this function and the browser resolve the hostname separately, so
    a DNS rebinding attacker controlling the authoritative server can return a
    public address here and a private one to Chromium. Deployments that navigate
    to untrusted URLs must additionally restrict egress at the network layer
    (VPC firewall rules, or an egress proxy the browser is pinned to). See the
    "Security Notes" section of the recipe README.
    """
    if not _validate_navigation_url(url):
        return False
    hostname = urllib.parse.urlparse(url).hostname
    if not hostname:
        return False
    hostname = _normalize_hostname(hostname)
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        # Hostname rather than an IP literal, so resolution is still needed.
        return await asyncio.to_thread(_resolved_addresses_allowed, hostname)
    return True


def _calculate_scroll_deltas(
    direction: Literal["up", "down", "left", "right"], magnitude: int
) -> tuple[int, int]:
    """Maps directional scroll names to (delta_x, delta_y) offsets."""
    if direction == "up":
        return 0, -magnitude
    elif direction == "down":
        return 0, magnitude
    elif direction == "left":
        return -magnitude, 0
    elif direction == "right":
        return magnitude, 0
    return 0, 0


def _format_url(url: str) -> str:
    """Validate and normalize an HTTP(S) URL."""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme}")
    return url


class MockBrowserComputer(BaseComputer):
    """Deterministic mock browser environment for offline testing and CI execution."""

    # 1x1 transparent PNG image bytes
    MOCK_SCREENSHOT_BYTES: bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0cIDATx\x9cc`\x00\x02\x00\x00\x05"
        b"\x00\x01z^\xab?\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def __init__(
        self,
        screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE,
        initial_url: str = DEFAULT_INITIAL_SEARCH_URL,
    ) -> None:
        self._screen_size = screen_size
        self._url = _format_url(initial_url)
        self._history: list[str] = [self._url]
        self._history_idx: int = 0

    def _visit(self, url: str) -> None:
        """Records a newly visited URL as the head of the history."""
        self._url = url
        self._history.append(url)
        self._history_idx = len(self._history) - 1

    async def screen_size(self) -> tuple[int, int]:
        return self._screen_size

    async def environment(self) -> ComputerEnvironment:
        return ComputerEnvironment.ENVIRONMENT_BROWSER

    async def open_web_browser(self) -> ComputerState:
        self._url = _format_url(self._url or DEFAULT_INITIAL_SEARCH_URL)
        return await self.current_state()

    async def click_at(self, x: int = 0, y: int = 0) -> ComputerState:
        return await self.current_state()

    async def hover_at(self, x: int = 0, y: int = 0) -> ComputerState:
        return await self.current_state()

    async def type_text_at(
        self,
        text: str = "",
        x: int | None = None,
        y: int | None = None,
        press_enter: bool = True,
        clear_before_typing: bool = True,
    ) -> ComputerState:
        clean_query = text.strip().replace(" ", "+")
        self._visit(
            _format_url(f"{GOOGLE_SHOPPING_SEARCH_URL}&q={clean_query}")
        )
        return await self.current_state()

    async def scroll_document(
        self, direction: Literal["up", "down", "left", "right"] = "down"
    ) -> ComputerState:
        return await self.current_state()

    async def scroll_at(
        self,
        direction: Literal["up", "down", "left", "right"] = "down",
        x: int | None = None,
        y: int | None = None,
        magnitude: int = DEFAULT_SCROLL_OFFSET,
    ) -> ComputerState:
        return await self.current_state()

    async def wait(self, seconds: int = 1) -> ComputerState:
        return await self.current_state()

    async def go_back(self) -> ComputerState:
        if self._history_idx > 0:
            self._history_idx -= 1
            self._url = self._history[self._history_idx]
        return await self.current_state()

    async def go_forward(self) -> ComputerState:
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            self._url = self._history[self._history_idx]
        return await self.current_state()

    async def search(self) -> ComputerState:
        self._visit(_format_url(GOOGLE_SHOPPING_SEARCH_URL))
        return await self.current_state()

    async def navigate(self, url: str) -> ComputerState:
        try:
            formatted_url = _format_url(url)
        except ValueError:
            # The model can emit any string here, so a bad scheme is an
            # expected outcome rather than a programming error.
            logger.warning("Rejected navigation to malformed URL: %s", url)
            return await self.current_state()
        if not await validate_navigation_target(formatted_url):
            logger.warning(
                "Rejected navigation to disallowed URL: %s", formatted_url
            )
            return await self.current_state()
        self._visit(formatted_url)
        return await self.current_state()

    async def key_combination(
        self, keys: list[str] | None = None, key: str | None = None
    ) -> ComputerState:
        return await self.current_state()

    async def drag_and_drop(
        self,
        x: int = 0,
        y: int = 0,
        destination_x: int = 0,
        destination_y: int = 0,
    ) -> ComputerState:
        return await self.current_state()

    async def current_state(self) -> ComputerState:
        return ComputerState(
            screenshot=self.MOCK_SCREENSHOT_BYTES,
            url=self._url,
        )


class PlaywrightBrowserComputer(BaseComputer):
    """Controls a browser session using Playwright for Gemini Computer Use."""

    def __init__(
        self,
        screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE,
        headless: bool = True,
    ) -> None:
        self._screen_size = screen_size
        self._headless = headless
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._current_session_id: str = "default"

    async def prepare(self, tool_context: Any) -> None:
        """Binds active session context to avoid multi-session page contention."""
        if (
            tool_context
            and hasattr(tool_context, "session")
            and tool_context.session
        ):
            self._current_session_id = str(tool_context.session.id)
        else:
            self._current_session_id = "default"

    @property
    def _page(self) -> Any:
        return self._pages.get(self._current_session_id)

    async def screen_size(self) -> tuple[int, int]:
        return self._screen_size

    async def environment(self) -> ComputerEnvironment:
        return ComputerEnvironment.ENVIRONMENT_BROWSER

    async def _ensure_browser(self) -> None:
        sid = self._current_session_id
        if sid not in self._pages or self._pages[sid] is None:
            try:
                from playwright.async_api import async_playwright

                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                if self._browser is None:
                    self._browser = await self._playwright.chromium.launch(
                        headless=self._headless
                    )
                if sid not in self._contexts or self._contexts[sid] is None:
                    self._contexts[sid] = await self._browser.new_context(
                        viewport={
                            "width": self._screen_size[0],
                            "height": self._screen_size[1],
                        }
                    )
                self._pages[sid] = await self._contexts[sid].new_page()
            except Exception as e:
                logger.warning(
                    "Playwright initialization failed (%s); falling back to dummy state",
                    e,
                )

    async def open_web_browser(self) -> ComputerState:
        await self._ensure_browser()
        if self._page and (
            not self._page.url or self._page.url == "about:blank"
        ):
            target_url = _format_url("https://www.google.com")
            await self._page.goto(target_url, wait_until="domcontentloaded")
        return await self.current_state()

    async def click_at(self, x: int = 0, y: int = 0) -> ComputerState:
        await self._ensure_browser()
        if self._page:
            await self._page.mouse.click(x, y)
            try:
                await self._page.wait_for_load_state(
                    "domcontentloaded", timeout=DEFAULT_TIMEOUT_MS
                )
            except Exception:
                pass
        return await self.current_state()

    async def hover_at(self, x: int = 0, y: int = 0) -> ComputerState:
        await self._ensure_browser()
        if self._page:
            await self._page.mouse.move(x, y)
        return await self.current_state()

    async def type_text_at(
        self,
        text: str = "",
        x: int | None = None,
        y: int | None = None,
        press_enter: bool = True,
        clear_before_typing: bool = True,
    ) -> ComputerState:
        await self._ensure_browser()
        if self._page:
            if x is not None and y is not None:
                await self._page.mouse.click(x, y)
            if clear_before_typing:
                await self._page.keyboard.press("Control+A")
                await self._page.keyboard.press("Backspace")
            await self._page.keyboard.type(text)
            if press_enter:
                await self._page.keyboard.press("Enter")
                try:
                    await self._page.wait_for_load_state(
                        "domcontentloaded", timeout=DEFAULT_TIMEOUT_MS
                    )
                except Exception:
                    pass
        return await self.current_state()

    async def scroll_document(
        self, direction: Literal["up", "down", "left", "right"] = "down"
    ) -> ComputerState:
        await self._ensure_browser()
        if self._page:
            delta_x, delta_y = _calculate_scroll_deltas(
                direction, DEFAULT_SCROLL_OFFSET
            )
            await self._page.mouse.wheel(delta_x, delta_y)
            await asyncio.sleep(POST_SCROLL_WAIT_SECONDS)
        return await self.current_state()

    async def scroll_at(
        self,
        direction: Literal["up", "down", "left", "right"] = "down",
        x: int | None = None,
        y: int | None = None,
        magnitude: int = DEFAULT_SCROLL_OFFSET,
    ) -> ComputerState:
        await self._ensure_browser()
        if self._page:
            if x is not None and y is not None:
                await self._page.mouse.move(x, y)
            delta_x, delta_y = _calculate_scroll_deltas(direction, magnitude)
            await self._page.mouse.wheel(delta_x, delta_y)
            await asyncio.sleep(POST_SCROLL_WAIT_SECONDS)
        return await self.current_state()

    async def wait(self, seconds: int = 1) -> ComputerState:
        await self._ensure_browser()
        await asyncio.sleep(min(seconds, MAX_WAIT_SECONDS))
        return await self.current_state()

    async def go_back(self) -> ComputerState:
        await self._ensure_browser()
        if self._page:
            try:
                await self._page.go_back(
                    wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS
                )
            except Exception:
                pass
        return await self.current_state()

    async def go_forward(self) -> ComputerState:
        await self._ensure_browser()
        if self._page:
            try:
                await self._page.go_forward(
                    wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS
                )
            except Exception:
                pass
        return await self.current_state()

    async def search(self) -> ComputerState:
        await self._ensure_browser()
        if self._page:
            target_url = _format_url(GOOGLE_SHOPPING_SEARCH_URL)
            await self._page.goto(
                target_url,
                wait_until="domcontentloaded",
            )
        return await self.current_state()

    async def navigate(self, url: str) -> ComputerState:
        try:
            formatted_url = _format_url(url)
        except ValueError:
            # The model can emit any string here, so a bad scheme is an
            # expected outcome rather than a programming error.
            logger.warning("Rejected navigation to malformed URL: %s", url)
            return await self.current_state()
        if not await validate_navigation_target(formatted_url):
            logger.warning(
                "Rejected navigation to disallowed URL: %s", formatted_url
            )
            return await self.current_state()
        await self._ensure_browser()
        if self._page:
            await self._page.goto(formatted_url, wait_until="domcontentloaded")
        return await self.current_state()

    async def key_combination(
        self, keys: list[str] | None = None, key: str | None = None
    ) -> ComputerState:
        await self._ensure_browser()
        combo = keys or ([key] if key else [])
        if self._page and combo:
            await self._page.keyboard.press("+".join(combo))
        return await self.current_state()

    async def drag_and_drop(
        self,
        x: int = 0,
        y: int = 0,
        destination_x: int = 0,
        destination_y: int = 0,
    ) -> ComputerState:
        await self._ensure_browser()
        if self._page:
            await self._page.mouse.move(x, y)
            await self._page.mouse.down()
            await self._page.mouse.move(destination_x, destination_y)
            await self._page.mouse.up()
        return await self.current_state()

    async def current_state(self) -> ComputerState:
        await self._ensure_browser()
        current_url = "about:blank"
        if self._page:
            current_url = self._page.url or "about:blank"
            try:
                screenshot_bytes = await self._page.screenshot(type="png")
                return ComputerState(
                    screenshot=screenshot_bytes,
                    url=self._page.url or current_url,
                )
            except Exception as e:
                logger.warning(
                    "Screenshot capture failed (%s); using fallback bytes", e
                )
        return ComputerState(
            screenshot=MockBrowserComputer.MOCK_SCREENSHOT_BYTES,
            url=current_url,
        )

    async def close(self) -> None:
        for page in self._pages.values():
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
        self._pages.clear()
        for context in self._contexts.values():
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
        self._contexts.clear()
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


def get_browser_computer() -> BaseComputer:
    """Returns PlaywrightBrowserComputer if enabled, otherwise MockBrowserComputer."""
    if constants.DISABLE_WEB_DRIVER:
        return MockBrowserComputer()
    try:
        import playwright  # noqa: F401

        return PlaywrightBrowserComputer()
    except ImportError:
        logger.info(
            "Playwright is not installed. Using MockBrowserComputer for offline mode."
        )
        return MockBrowserComputer()


def get_computer_use_toolset() -> ComputerUseToolset:
    """Creates a ComputerUseToolset wrapping the configured browser computer."""
    computer = get_browser_computer()
    return ComputerUseToolset(computer=computer)
