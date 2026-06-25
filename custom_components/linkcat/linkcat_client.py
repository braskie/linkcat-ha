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
        # The Linkcat home page renders login mostly through JS; the account URL returns
        # a server-rendered login form that can be parsed without browser automation.
        login_page_html = await _request_text(session, "GET", self._account_url)
        login_form = _extract_login_form(login_page_html)
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
            headers={"Referer": self._account_url},
        )

        if _contains_explicit_auth_failure(login_html):
            _LOGGER.debug("Linkcat login POST returned explicit auth failure text")
            raise LinkcatAuthError("Linkcat login failed. Check username/password.")

        account_html = await _request_text(
            session,
            "GET",
            self._account_url,
            headers={"Referer": login_url},
        )

        if _looks_logged_out(account_html):
            _LOGGER.debug("Linkcat account page still contains login form fields after submit")
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


def _contains_explicit_auth_failure(html_text: str) -> bool:
    lowered = _html_to_text(html_text).lower()
    failure_markers = [
        "invalid login",
        "invalid password",
        "incorrect",
        "unsuccessful",
        "authentication failed",
        "login failed",
        "unable to authenticate",
    ]
    return any(marker in lowered for marker in failure_markers)


def _looks_logged_out(account_html: str) -> bool:
    lowered = account_html.lower()
    has_account_indicators = any(
        marker in lowered
        for marker in (
            "librarycheckoutstable",
            "holdstab",
            "items checked out",
            "items on hold",
            "ready for pickup",
            "my account",
        )
    )

    if has_account_indicators:
        return False

    # Login-page specific markers are more reliable than generic j_username fields,
    # which may appear in unrelated forms on authenticated pages.
    return (
        "id=\"loginpageform\"" in lowered
        or "patronloginform.loginpageform" in lowered
        or (
            ("name=\"j_username\"" in lowered or "id=\"j_username\"" in lowered)
            and ("name=\"j_password\"" in lowered or "id=\"j_password\"" in lowered)
        )
    )


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
        # Skip header rows (only <th> cells, no <td>)
        if not re.search(r"<td\b", row_html, re.IGNORECASE):
            continue
        title = None
        author = None
        title_cell_idx = None
        for idx, cell in enumerate(cells):
            anchor = _extract_anchor_text(cell)
            if anchor and "click to sort" not in anchor.lower():
                title = anchor
                title_cell_idx = idx
                break

        # Fallback: multiline parse across cells
        if not title:
            for idx, cell in enumerate(cells):
                t, a = _parse_title_author_from_multiline_text(_html_to_text(cell))
                if t and "click to sort" not in t.lower():
                    title, author = t, a
                    title_cell_idx = idx
                    break

        # Extract author from the same cell as the title
        if title and title_cell_idx is not None and author is None:
            _, author = _parse_title_author_from_multiline_text(_html_to_text(cells[title_cell_idx]))

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

        row_text = _html_to_text(row_html)
        if "click to sort" in row_text.lower():
            continue
        # Skip header rows (only <th> cells, no <td>)
        if not re.search(r"<td\b", row_html, re.IGNORECASE):
            continue
        title = None
        author = None
        title_cell_idx = None
        for idx, cell in enumerate(cells):
            anchor = _extract_anchor_text(cell)
            if anchor and "title/author" not in anchor.lower():
                title = anchor
                title_cell_idx = idx
                break

        # Fallback: multiline parse across cells
        if not title:
            for idx, cell in enumerate(cells):
                t, a = _parse_title_author_from_multiline_text(_html_to_text(cell))
                if t and "title/author" not in t.lower():
                    title, author = t, a
                    title_cell_idx = idx
                    break

        # Extract author from the same cell as the title
        if title and title_cell_idx is not None and author is None:
            _, author = _parse_title_author_from_multiline_text(_html_to_text(cells[title_cell_idx]))

        # Status is the next cell after the title, with cells[2] as a fallback
        status = None
        if title_cell_idx is not None and title_cell_idx + 1 < len(cells):
            status = _html_to_text(cells[title_cell_idx + 1]).strip() or None
        if not status and len(cells) > 2:
            status = _html_to_text(cells[2]).strip() or None

        ready = _is_hold_ready(status or row_text)
        image_url = _extract_image_url(row_html, base_url)

        if title and "title/author" not in title.lower():
            holds.append(HoldItem(title=title, author=author, image_url=image_url, status=status, ready=ready))

    return _dedupe_holds(holds)


def _extract_balanced_tag_content(html: str, tag: str, content_start: int) -> str | None:
    """Return the content from content_start up to the matching closing tag, handling nesting."""
    depth = 1
    pos = content_start
    open_re = re.compile(rf"<{re.escape(tag)}\b", re.IGNORECASE)
    close_re = re.compile(rf"</{re.escape(tag)}\s*>", re.IGNORECASE)
    while depth > 0:
        open_m = open_re.search(html, pos)
        close_m = close_re.search(html, pos)
        if close_m is None:
            return None
        if open_m is not None and open_m.start() < close_m.start():
            depth += 1
            pos = open_m.end()
        else:
            depth -= 1
            if depth == 0:
                return html[content_start : close_m.start()]
            pos = close_m.end()
    return None


def _find_table(account_html: str, id_hints: list[str]) -> str | None:
    # Primary: look for <table id="...hint..."> and extract its full content,
    # properly accounting for nested <table> tags so the match is not truncated.
    for id_hint in id_hints:
        open_m = re.search(
            rf"<table\b[^>]*id=['\"][^'\"]*{re.escape(id_hint)}[^'\"]*['\"][^>]*>",
            account_html,
            flags=re.IGNORECASE,
        )
        if open_m:
            content = _extract_balanced_tag_content(account_html, "table", open_m.end())
            if content is not None:
                _LOGGER.debug("Found table by id hint '%s'", id_hint)
                return content

    # Fallback: look for a section <div id="...hint..."> and find the first table inside it.
    for id_hint in id_hints:
        open_m = re.search(
            rf"<div\b[^>]*id=['\"][^'\"]*{re.escape(id_hint)}[^'\"]*['\"][^>]*>",
            account_html,
            flags=re.IGNORECASE,
        )
        if open_m:
            div_content = _extract_balanced_tag_content(account_html, "div", open_m.end())
            if div_content:
                table_open_m = re.search(r"<table\b[^>]*>", div_content, flags=re.IGNORECASE)
                if table_open_m:
                    content = _extract_balanced_tag_content(div_content, "table", table_open_m.end())
                    if content is not None:
                        _LOGGER.debug("Found table via div id hint '%s'", id_hint)
                        return content

    _LOGGER.debug("No table found for id hints: %s", id_hints)
    return None


def _extract_table_rows(table_html: str) -> list[str]:
    """Extract top-level <tr> row contents, skipping rows inside nested <table> elements."""
    return _extract_direct_tag_contents(table_html, "tr")


def _extract_cells(row_html: str) -> list[str]:
    """Extract top-level <td>/<th> cell contents, skipping cells from nested tables."""
    # Collect both td and th in document order
    td_results = _extract_direct_tag_contents(row_html, "td")
    th_results = _extract_direct_tag_contents(row_html, "th")
    if not th_results:
        return td_results
    if not td_results:
        return th_results

    # Interleave by finding tag positions
    combined = []
    pos = 0
    td_pat = re.compile(r"<td\b[^>]*>", re.IGNORECASE)
    th_pat = re.compile(r"<th\b[^>]*>", re.IGNORECASE)
    td_iter = iter(td_results)
    th_iter = iter(th_results)
    next_td = next(td_iter, None)
    next_th = next(th_iter, None)
    while True:
        td_m = td_pat.search(row_html, pos) if next_td is not None else None
        th_m = th_pat.search(row_html, pos) if next_th is not None else None
        if td_m is None and th_m is None:
            break
        if th_m is None or (td_m is not None and td_m.start() < th_m.start()):
            combined.append(next_td)
            next_td = next(td_iter, None)
            pos = td_m.end()
        else:
            combined.append(next_th)
            next_th = next(th_iter, None)
            pos = th_m.end()
    return combined


def _extract_direct_tag_contents(html: str, tag: str) -> list[str]:
    """Extract content of top-level <tag> elements, skipping same-named tags inside nested <table> blocks."""
    results = []
    pos = 0
    open_table_re = re.compile(r"<table\b", re.IGNORECASE)
    close_table_re = re.compile(r"</table\s*>", re.IGNORECASE)
    open_tag_re = re.compile(rf"<{re.escape(tag)}\b[^>]*>", re.IGNORECASE)
    close_tag_re = re.compile(rf"</{re.escape(tag)}\s*>", re.IGNORECASE)

    while pos < len(html):
        open_table_m = open_table_re.search(html, pos)
        open_tag_m = open_tag_re.search(html, pos)

        if open_tag_m is None:
            break

        if open_table_m is not None and open_table_m.start() < open_tag_m.start():
            # Skip nested table before this tag.
            nested_content = _extract_balanced_tag_content(html, "table", open_table_m.end())
            if nested_content is not None:
                skip_end = open_table_m.end() + len(nested_content)
                close_m = close_table_re.search(html, skip_end)
                pos = close_m.end() if close_m else skip_end
            else:
                pos = open_table_m.end()
            continue

        # Extract content up to the matching close tag, skipping nested tables.
        content = _extract_content_to_close_tag(html, open_tag_m.end(), close_tag_re, close_table_re)
        if content is not None:
            results.append(content)
            end_pos = open_tag_m.end() + len(content)
            close_m = close_tag_re.search(html, end_pos)
            pos = close_m.end() if close_m else end_pos
        else:
            pos = open_tag_m.end()

    return results


def _extract_content_to_close_tag(
    html: str,
    start: int,
    close_re: re.Pattern,
    close_table_re: re.Pattern,
) -> str | None:
    """Return content from `start` to the first `close_re` match, skipping nested <table> blocks."""
    pos = start
    open_table_re = re.compile(r"<table\b", re.IGNORECASE)

    while pos < len(html):
        open_table_m = open_table_re.search(html, pos)
        close_m = close_re.search(html, pos)

        if close_m is None:
            return None

        if open_table_m is None or close_m.start() <= open_table_m.start():
            return html[start : close_m.start()]

        # Nested table precedes the close tag; skip it.
        nested_content = _extract_balanced_tag_content(html, "table", open_table_m.end())
        if nested_content is None:
            return None
        skip_end = open_table_m.end() + len(nested_content)
        close_table_m = close_table_re.search(html, skip_end)
        pos = close_table_m.end() if close_table_m else skip_end

    return None


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


def _is_hold_ready(text: str) -> bool:
    lowered = text.lower()
    if any(blocker in lowered for blocker in ("pending", "in process", "queued", "in transit")):
        return False
    return any(keyword in lowered for keyword in ("ready", "pickup", "available", "download available"))
