"""HTTP client for scraping Linkcat account pages without browser automation."""

from __future__ import annotations

import html
import logging
import re
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .const import BASE_URL
from .models import CheckoutItem, HoldItem, LinkcatAccountData

_LOGGER = logging.getLogger(__name__)

class LinkcatAuthError(Exception):
    """Raised when Linkcat authentication fails."""


class LinkcatConnectionError(Exception):
    """Raised when Linkcat could not be reached or parsed."""


class LinkcatClient:
    """Client used to login and scrape account data from Linkcat."""

    def __init__(self, username: str, password: str, base_url: str = BASE_URL) -> None:
        self._username = username
        self._password = password
        self._base_url = base_url
        self._account_url = urljoin(base_url, "search/account?")

    async def fetch_account_data(self) -> LinkcatAccountData:
        """Log in and scrape checkouts and holds from Linkcat."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            account_html = await self._login_and_get_account_page(session)
            return self._parse_account_data(account_html)

    async def validate_credentials(self) -> None:
        """Validate credentials by attempting login."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            await self._login_and_get_account_page(session)

    async def _login_and_get_account_page(self, session: aiohttp.ClientSession) -> str:
        home_html = await _request_text(session, "GET", self._base_url)
        login_form = _extract_login_form(home_html)
        if login_form is None:
            raise LinkcatConnectionError("Could not find Linkcat login form.")

        payload = dict(login_form["inputs"])
        payload["j_username"] = self._username
        payload["j_password"] = self._password
        if "submit_0" in payload and not payload["submit_0"]:
            payload["submit_0"] = "Log In"

        login_url = urljoin(self._base_url, str(login_form["action"]))
        login_html = await _request_text(
            session,
            "POST",
            login_url,
            data=payload,
            headers={"Referer": self._base_url},
        )

        if _contains_auth_failure(login_html):
            raise LinkcatAuthError("Linkcat login failed. Check username/password.")

        account_html = await _request_text(
            session,
            "GET",
            self._account_url,
            headers={"Referer": login_url},
        )

        if _looks_logged_out(account_html):
            raise LinkcatAuthError("Linkcat login failed. Check username/password.")

        return account_html

    def _parse_account_data(self, account_html: str) -> LinkcatAccountData:
        body_text = _html_to_text(account_html)

        checkouts = _extract_checkout_rows(account_html, self._base_url)
        holds = _extract_hold_rows(account_html, self._base_url)

        checkout_count = _extract_count(body_text, ["checkouts", "checked out", "items out"])
        hold_count = _extract_count(body_text, ["holds", "on hold"])
        ready_hold_count = _extract_count(
            body_text,
            ["ready holds", "ready for pickup", "ready for download", "available holds"],
        )

        if checkout_count is not None and not checkouts:
            checkouts = [CheckoutItem(title=f"Checkout {idx + 1}") for idx in range(checkout_count)]

        if hold_count is not None and not holds:
            holds = [HoldItem(title=f"Hold {idx + 1}", ready=(idx < (ready_hold_count or 0))) for idx in range(hold_count)]

        if ready_hold_count is not None and holds:
            for idx, hold in enumerate(holds):
                hold.ready = idx < ready_hold_count

        return LinkcatAccountData(checkouts=checkouts, holds=holds)


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


async def _request_text(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    try:
        async with session.request(method, url, data=data, headers=headers, allow_redirects=True) as response:
            response.raise_for_status()
            return await response.text()
    except aiohttp.ClientError as exc:
        raise LinkcatConnectionError(f"Failed communicating with Linkcat: {exc}") from exc


def _extract_login_form(html_text: str) -> dict[str, Any] | None:
    form_match = re.search(
        r"(<form[^>]*id=['\"]loginPageForm['\"][^>]*>)(.*?)</form>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not form_match:
        form_match = re.search(
            r"(<form[^>]*action=['\"][^'\"]*patronloginform\.loginpageform[^'\"]*['\"][^>]*>)(.*?)</form>",
            html_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if not form_match:
        return None

    opening_tag, inner_html = form_match.group(1), form_match.group(2)
    action = _extract_attr(opening_tag, "action")
    if not action:
        return None

    inputs: dict[str, str] = {}
    for input_tag in re.findall(r"<input\b[^>]*>", inner_html, flags=re.IGNORECASE):
        name = _extract_attr(input_tag, "name")
        if not name:
            continue
        input_type = (_extract_attr(input_tag, "type") or "text").lower()
        if input_type == "button":
            continue
        value = _extract_attr(input_tag, "value") or ""
        inputs[name] = value

    return {"action": action, "inputs": inputs}


def _extract_attr(tag_html: str, attr_name: str) -> str | None:
    match = re.search(rf"\b{re.escape(attr_name)}\s*=\s*['\"]([^'\"]*)['\"]", tag_html, flags=re.IGNORECASE)
    if not match:
        return None
    return html.unescape(match.group(1)).strip()


def _contains_auth_failure(html_text: str) -> bool:
    lowered = _html_to_text(html_text).lower()
    failure_markers = [
        "invalid login",
        "invalid password",
        "incorrect",
        "unsuccessful",
        "authentication failed",
        "login failed",
        "login/barcode",
        "password/pin",
    ]
    return any(marker in lowered for marker in failure_markers)


def _looks_logged_out(account_html: str) -> bool:
    lowered = account_html.lower()
    return "name=\"j_username\"" in lowered or "id=\"j_username\"" in lowered


def _html_to_text(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|td|th|tr|h1|h2|h3|h4|h5|h6)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\r", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _extract_checkout_rows(account_html: str, base_url: str) -> list[CheckoutItem]:
    table_html = _find_table(account_html, ["libraryCheckoutsTable", "checkoutsTab"])
    if not table_html:
        return []

    checkouts: list[CheckoutItem] = []
    for row_html in _extract_table_rows(table_html):
        cells = _extract_cells(row_html)
        if len(cells) < 2:
            continue

        row_text = _html_to_text(row_html)
        if "click to sort" in row_text.lower():
            continue

        title = _extract_anchor_text(cells[0]) or _extract_anchor_text(cells[1])
        author = None
        if len(cells) > 2:
            parsed_title, parsed_author = _parse_title_author_from_multiline_text(_html_to_text(cells[2]))
            title = title or parsed_title
            author = parsed_author

        if not title:
            title = row_text.split("Shelf Number:", maxsplit=1)[0].strip()

        image_url = _extract_image_url(row_html, base_url)
        due_date = _extract_due_date(_html_to_text(cells[-1])) or _extract_due_date(row_text)

        if title:
            checkouts.append(CheckoutItem(title=title, author=author, image_url=image_url, due_date=due_date))

    return _dedupe_checkouts(checkouts)


def _extract_hold_rows(account_html: str, base_url: str) -> list[HoldItem]:
    table_html = _find_table(account_html, ["holdsTab"])
    if not table_html:
        return []

    holds: list[HoldItem] = []
    for row_html in _extract_table_rows(table_html):
        cells = _extract_cells(row_html)
        if len(cells) < 2:
            continue

        title, author = _parse_title_author_from_multiline_text(_html_to_text(cells[1]))
        if not title:
            title = _html_to_text(cells[1])

        status = _html_to_text(cells[2]) if len(cells) > 2 else _html_to_text(row_html)
        ready = _is_hold_ready(status)
        image_url = _extract_image_url(row_html, base_url)

        if title and "title/author" not in title.lower():
            holds.append(HoldItem(title=title, author=author, image_url=image_url, status=status, ready=ready))

    return _dedupe_holds(holds)


def _find_table(account_html: str, id_hints: list[str]) -> str | None:
    for id_hint in id_hints:
        match = re.search(
            rf"<table[^>]*id=['\"][^'\"]*{re.escape(id_hint)}[^'\"]*['\"][^>]*>(.*?)</table>",
            account_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1)

    # Fallback: search for any table within a section div matching id hints.
    for id_hint in id_hints:
        section_match = re.search(
            rf"<div[^>]*id=['\"][^'\"]*{re.escape(id_hint)}[^'\"]*['\"][^>]*>(.*?)</div>",
            account_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if section_match:
            table_match = re.search(r"<table[^>]*>(.*?)</table>", section_match.group(1), flags=re.IGNORECASE | re.DOTALL)
            if table_match:
                return table_match.group(1)

    return None


def _extract_table_rows(table_html: str) -> list[str]:
    return re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL)


def _extract_cells(row_html: str) -> list[str]:
    return re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, flags=re.IGNORECASE | re.DOTALL)


def _extract_anchor_text(cell_html: str) -> str | None:
    anchor_match = re.search(r"<a\b[^>]*>(.*?)</a>", cell_html, flags=re.IGNORECASE | re.DOTALL)
    if not anchor_match:
        return None
    text = _html_to_text(anchor_match.group(1)).strip()
    return text or None


def _extract_image_url(fragment_html: str, base_url: str) -> str | None:
    image_match = re.search(r"<img\b[^>]*\bsrc=['\"]([^'\"]+)['\"]", fragment_html, flags=re.IGNORECASE)
    if not image_match:
        return None
    return urljoin(base_url, html.unescape(image_match.group(1)).strip())


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


def _is_hold_ready(status_text: str) -> bool:
    lowered = status_text.lower()
    return any(marker in lowered for marker in ["ready", "available", "pickup", "download"])


def _is_hold_ready(text: str) -> bool:
    lowered = text.lower()
    if any(blocker in lowered for blocker in ("pending", "in process", "queued", "in transit")):
        return False
    return any(keyword in lowered for keyword in ("ready", "pickup", "available", "download available"))
