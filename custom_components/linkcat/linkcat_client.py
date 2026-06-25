"""Playwright client for scraping Linkcat account pages."""

from __future__ import annotations

import importlib
import logging
import re
from typing import Any
from urllib.parse import urljoin

from .const import BASE_URL
from .models import CheckoutItem, HoldItem, LinkcatAccountData

_LOGGER = logging.getLogger(__name__)

Browser = Any
BrowserContext = Any
Page = Any


class LinkcatAuthError(Exception):
    """Raised when Linkcat authentication fails."""


class LinkcatClient:
    """Client used to scrape account data from Linkcat."""

    def __init__(self, username: str, password: str, base_url: str = BASE_URL) -> None:
        self._username = username
        self._password = password
        self._base_url = base_url

    async def fetch_account_data(self) -> LinkcatAccountData:
        """Log in and scrape checkouts and holds from Linkcat."""
        playwright_async_api = _import_playwright_async_api()
        async with playwright_async_api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            try:
                page = await context.new_page()
                await self._login(page)
                data = await self._scrape_account(page)
                return data
            finally:
                await _safe_close(context, browser)

    async def validate_credentials(self) -> None:
        """Validate credentials by attempting login."""
        playwright_async_api = _import_playwright_async_api()
        async with playwright_async_api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()

            try:
                page = await context.new_page()
                await self._login(page)
            finally:
                await _safe_close(context, browser)

    async def _login(self, page: Page) -> None:
        await page.goto(self._base_url, wait_until="domcontentloaded", timeout=60000)

        login_link_selectors = [
            "a[href*='login']",
            "button:has-text('Log In')",
            "button:has-text('Login')",
            "text=Log In",
        ]
        for selector in login_link_selectors:
            if await page.locator(selector).count() > 0:
                await page.locator(selector).first.click()
                break

        login_dialog = page.get_by_role("dialog", name=re.compile(r"log in", re.IGNORECASE))
        try:
            await login_dialog.wait_for(state="visible", timeout=10000)
        except Exception:
            pass

        username_locator = login_dialog.locator("#j_username, input[name='j_username']").first
        password_locator = login_dialog.locator("#j_password, input[name='j_password']").first

        if username_locator is None or password_locator is None:
            raise LinkcatAuthError("Could not find login form fields on Linkcat page.")

        await username_locator.fill(self._username)
        await password_locator.fill(self._password)

        submit_locator = login_dialog.locator("#submit_0, input[name='submit_0'], button[type='submit'], input[type='submit']").first
        if await submit_locator.count() > 0:
            await submit_locator.click()
        else:
            await password_locator.press("Enter")

        try:
            await page.wait_for_url(re.compile(r"/search/account\??$"), timeout=15000)
        except Exception:
            await page.wait_for_timeout(2500)

        if await page.locator("button:has-text('Close')").count() > 0:
            try:
                await page.locator("button:has-text('Close')").first.click()
            except Exception:
                _LOGGER.debug("Login modal close click failed", exc_info=True)

        if await self._is_auth_failed(page):
            raise LinkcatAuthError("Linkcat login failed. Check username/password.")

    async def _is_auth_failed(self, page: Page) -> bool:
        failure_markers = [
            "invalid login",
            "invalid password",
            "incorrect",
            "unsuccessful",
            "try again",
            "authentication failed",
            "login failed",
        ]

        login_dialog = page.get_by_role("dialog", name=re.compile(r"log in", re.IGNORECASE))
        try:
            if await page.locator("text=My Account").count() > 0 or await page.locator("text=Checkouts").count() > 0:
                return False
        except Exception:
            _LOGGER.debug("Failed checking account hints", exc_info=True)

        visible_text = ""
        try:
            if await login_dialog.count() > 0:
                visible_text = (await login_dialog.first.inner_text()).lower()
        except Exception:
            _LOGGER.debug("Failed reading login dialog text", exc_info=True)

        if any(marker in visible_text for marker in failure_markers):
            return True

        try:
            if await login_dialog.count() > 0 and await login_dialog.first.is_visible():
                return True
        except Exception:
            _LOGGER.debug("Failed checking login dialog visibility", exc_info=True)

        return False

    async def _scrape_account(self, page: Page) -> LinkcatAccountData:
        account_selectors = [
            "a[href*='account']",
            "text=My Account",
            "text=Checkouts",
            "text=Holds",
        ]
        for selector in account_selectors:
            if await page.locator(selector).count() > 0:
                try:
                    await page.locator(selector).first.click()
                    break
                except Exception:
                    continue

        await page.wait_for_timeout(1500)

        body_text = await page.locator("body").inner_text()

        checkout_count = await _extract_linkcat_summary_value(
            page,
            [
                ("Checkouts", "Library"),
                ("Checkouts", "Digital"),
                ("Total Items Checked Out", None),
            ],
        )
        hold_count = await _extract_linkcat_summary_value(
            page,
            [
                ("Holds", "Library"),
                ("Holds", "Digital"),
                ("Items on Hold", None),
            ],
        )
        ready_hold_count = await _extract_linkcat_summary_value(
            page,
            [
                ("Ready for Pickup", None),
                ("Ready for Download", None),
                ("Ready Holds", None),
            ],
        )

        if checkout_count is None:
            checkout_count = _extract_count(body_text, ["checkouts", "checked out", "items out"])
        if hold_count is None:
            hold_count = _extract_count(body_text, ["holds", "on hold"])
        if ready_hold_count is None:
            ready_hold_count = _extract_count(
                body_text,
                ["ready holds", "ready for pickup", "ready for download", "available holds"],
            )

        checkouts = await _extract_checkout_rows(page)
        holds = await _extract_hold_rows(page)

        if checkout_count is not None and not checkouts:
            checkouts = [CheckoutItem(title=f"Checkout {i + 1}") for i in range(checkout_count)]

        if hold_count is not None and not holds:
            holds = [HoldItem(title=f"Hold {i + 1}", ready=(i < (ready_hold_count or 0))) for i in range(hold_count)]

        if ready_hold_count is not None and holds:
            for index, item in enumerate(holds):
                item.ready = index < ready_hold_count

        return LinkcatAccountData(checkouts=checkouts, holds=holds)


def _import_playwright_async_api() -> Any:
    try:
        return importlib.import_module("playwright.async_api")
    except ImportError as exc:
        raise LinkcatAuthError(
            "Playwright is not installed. Install integration requirements and browser dependencies."
        ) from exc


async def _first_existing_locator(page: Page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count() > 0:
            return locator.first
    return None


async def _safe_close(context: BrowserContext, browser: Browser) -> None:
    try:
        await context.close()
    except Exception:
        _LOGGER.debug("Failed closing Playwright context", exc_info=True)

    try:
        await browser.close()
    except Exception:
        _LOGGER.debug("Failed closing Playwright browser", exc_info=True)


def _extract_count(text: str, labels: list[str]) -> int | None:
    normalized = " ".join(text.split())
    for label in labels:
        pattern = rf"(?:{re.escape(label)}[^0-9]{{0,20}})(\d+)"
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))

        pattern = rf"(\d+)(?:[^A-Za-z]{{0,10}}{re.escape(label)})"
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


async def _extract_checkout_rows(page: Page) -> list[CheckoutItem]:
    checkout_rows = await _extract_linkcat_checkout_rows(page)
    if checkout_rows:
        return _dedupe_checkouts(checkout_rows)

    rows: list[CheckoutItem] = []
    candidate_rows = page.locator("tr")
    row_count = min(await candidate_rows.count(), 100)

    for idx in range(row_count):
        row_text = (await candidate_rows.nth(idx).inner_text()).strip()
        if not row_text:
            continue
        lower = row_text.lower()
        if "due" not in lower and "checkout" not in lower and "checked out" not in lower:
            continue

        title = row_text.split("\n", maxsplit=1)[0].strip()
        due_date = _extract_due_date(row_text)
        rows.append(CheckoutItem(title=title, due_date=due_date))

    return _dedupe_checkouts(rows)


async def _extract_hold_rows(page: Page) -> list[HoldItem]:
    hold_rows = await _extract_linkcat_hold_rows(page)
    if hold_rows:
        return _dedupe_holds(hold_rows)

    rows: list[HoldItem] = []
    candidate_rows = page.locator("tr")
    row_count = min(await candidate_rows.count(), 100)

    for idx in range(row_count):
        row_text = (await candidate_rows.nth(idx).inner_text()).strip()
        if not row_text:
            continue
        lower = row_text.lower()
        if "hold" not in lower and "pickup" not in lower and "available" not in lower:
            continue

        title = row_text.split("\n", maxsplit=1)[0].strip()
        ready = any(word in lower for word in ("ready", "available", "pickup"))
        rows.append(HoldItem(title=title, status=row_text, ready=ready))

    return _dedupe_holds(rows)


def _extract_due_date(text: str) -> str | None:
    date_patterns = [
        r"due\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        r"due\s*([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


async def _extract_linkcat_summary_value(page: Page, strategies: list[tuple[str, str | None]]) -> int | None:
    for section_name, label in strategies:
        section = page.locator(f"div:has(> a:has-text('{section_name}'), > span:has-text('{section_name}'))").first
        if await section.count() > 0:
            section_text = " ".join((await section.inner_text()).split())

            if label is None:
                value = _extract_count(section_text, [section_name])
                if value is not None:
                    return value
            else:
                value = _extract_count(section_text, [label])
                if value is not None:
                    return value

    headings = page.locator("h1, h2, h3, h4")
    heading_count = min(await headings.count(), 60)
    for idx in range(heading_count):
        text = " ".join((await headings.nth(idx).inner_text()).split())
        for section_name, label in strategies:
            if section_name.lower() not in text.lower():
                continue

            if label is None:
                value = _extract_count(text, [section_name])
                if value is not None:
                    return value
            else:
                value = _extract_count(text, [label])
                if value is not None:
                    return value

    return None


async def _extract_linkcat_checkout_rows(page: Page) -> list[CheckoutItem]:
    selectors = [
        "#libraryCheckoutsTable tbody tr",
        "#checkoutsTab table tbody tr",
    ]
    rows: list[CheckoutItem] = []

    for selector in selectors:
        locator = page.locator(selector)
        count = min(await locator.count(), 100)
        if count == 0:
            continue

        for idx in range(count):
            row = locator.nth(idx)
            row_text = " ".join((await row.inner_text()).split())
            if not row_text:
                continue

            title_locator = row.locator("a[href*='detailnonmodal']").first
            title = ""
            author = None
            if await title_locator.count() > 0:
                title = (await title_locator.inner_text()).strip()

            image_url = await _extract_row_image_url(page, row)

            cells = row.locator("td")
            cell_count = await cells.count()
            due_date = None
            if cell_count > 0:
                due_date = _extract_due_date(" ".join((await cells.nth(cell_count - 1).inner_text()).split()))

            if cell_count > 2:
                title_author_text = await cells.nth(2).inner_text()
                parsed_title, parsed_author = _parse_title_author_from_multiline_text(title_author_text)
                if not title:
                    title = parsed_title
                author = parsed_author or author

            if not title:
                title = row_text.split("Shelf Number:", maxsplit=1)[0].strip()

            if title and "click to sort" not in title.lower():
                rows.append(
                    CheckoutItem(
                        title=title,
                        author=author,
                        image_url=image_url,
                        due_date=due_date or _extract_due_date(row_text),
                    )
                )

        if rows:
            return rows

    return rows


async def _extract_linkcat_hold_rows(page: Page) -> list[HoldItem]:
    selectors = [
        "#holdsTab table tbody tr",
        "#holdsTab table tr",
    ]
    rows: list[HoldItem] = []

    for selector in selectors:
        locator = page.locator(selector)
        count = min(await locator.count(), 100)
        if count == 0:
            continue

        for idx in range(count):
            row = locator.nth(idx)
            cells = row.locator("td")
            cell_count = await cells.count()
            if cell_count < 2:
                continue

            title_author_text = await cells.nth(1).inner_text()
            title, author = _parse_title_author_from_multiline_text(title_author_text)
            if not title:
                title = " ".join(title_author_text.split())

            image_url = await _extract_row_image_url(page, row)

            status = ""
            if cell_count > 2:
                status = " ".join((await cells.nth(2).inner_text()).split())

            row_text = " ".join((await row.inner_text()).split())
            ready = _is_hold_ready(status or row_text)

            if title and "title/author" not in title.lower():
                rows.append(
                    HoldItem(
                        title=title,
                        author=author,
                        image_url=image_url,
                        status=status or row_text,
                        ready=ready,
                    )
                )

        if rows:
            return rows

    return rows


def _dedupe_checkouts(items: list[CheckoutItem]) -> list[CheckoutItem]:
    seen: set[tuple[str, str | None, str | None, str | None]] = set()
    deduped: list[CheckoutItem] = []
    for item in items:
        key = (item.title, item.author, item.image_url, item.due_date)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _dedupe_holds(items: list[HoldItem]) -> list[HoldItem]:
    seen: set[tuple[str, str | None, str | None, str | None, bool]] = set()
    deduped: list[HoldItem] = []
    for item in items:
        key = (item.title, item.author, item.image_url, item.status, item.ready)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


async def _extract_row_image_url(page: Page, row: Any) -> str | None:
    image = row.locator("img[src]").first
    if await image.count() == 0:
        return None
    src = await image.get_attribute("src")
    if not src:
        return None
    return urljoin(str(page.url), src)


def _parse_title_author_from_multiline_text(text: str) -> tuple[str, str | None]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "", None

    title = lines[0]
    author = None
    if len(lines) > 1:
        for line in lines[1:]:
            lowered = line.lower()
            if lowered.startswith("shelf number:") or lowered.startswith("item barcode:"):
                continue
            author = line
            break

    return title, author


def _is_hold_ready(text: str) -> bool:
    lowered = text.lower()
    if any(blocker in lowered for blocker in ("pending", "in process", "queued", "in transit")):
        return False
    return any(keyword in lowered for keyword in ("ready", "pickup", "available", "download available"))
