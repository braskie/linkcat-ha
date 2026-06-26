"""HTTP client for scraping Linkcat account pages without browser automation."""

from __future__ import annotations

import html
import json
import logging
import os
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
            subpages = await self._fetch_account_subpages(session, account_html)
            return self._parse_account_data([account_html, *subpages])

    async def validate_credentials(self) -> None:
        """Validate credentials by attempting login."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            await self._login_and_get_account_page(session)

    async def _login_and_get_account_page(self, session: aiohttp.ClientSession) -> str:
        # The account URL returns a server-rendered login form that can be parsed
        # without browser automation.
        login_page_html = await _request_text(session, "GET", self._account_url)
        _debug_dump_html("login_page", login_page_html)

        login_form = _extract_login_form(login_page_html)
        if login_form is None:
            raise LinkcatConnectionError("Could not find Linkcat login form.")

        payload = dict(login_form["inputs"])
        payload["j_username"] = self._username
        payload["j_password"] = self._password
        if "submit_0" in payload and not payload["submit_0"]:
            payload["submit_0"] = "Log In"

        csrf_token = payload.get("sdcsrf", "")

        login_url = urljoin(self._base_url, str(login_form["action"]))
        login_html = await _request_text(
            session,
            "POST",
            login_url,
            data=payload,
            headers={
                "Referer": self._account_url,
                "sdcsrf": csrf_token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/html, */*; q=0.01",
            },
        )
        _debug_dump_html("post_login", login_html)

        if _contains_explicit_auth_failure(login_html):
            _LOGGER.debug("Linkcat login POST returned explicit auth failure text")
            raise LinkcatAuthError("Linkcat login failed. Check username/password.")

        account_html = await _request_text(
            session,
            "GET",
            self._account_url,
            headers={"Referer": login_url},
        )
        _debug_dump_html("account", account_html)

        if _looks_logged_out(account_html):
            _LOGGER.debug("Linkcat account page still contains login form fields after submit")
            raise LinkcatAuthError("Linkcat login failed. Check username/password.")

        return account_html

    async def _fetch_account_subpages(self, session: aiohttp.ClientSession, account_html: str) -> list[str]:
        candidate_urls: list[str] = [
            urljoin(self._base_url, "search/account/checkouts?"),
            urljoin(self._base_url, "search/account/holds?"),
            urljoin(self._base_url, "search/account/dashboard?"),
        ]

        progressive_urls = _extract_progressive_display_urls(account_html)
        for progressive_url in progressive_urls:
            candidate_urls.append(urljoin(self._base_url, progressive_url))

        for href in _extract_account_links(account_html):
            candidate_urls.append(urljoin(self._base_url, href))

        seen: set[str] = set()
        pages: list[str] = []
        for url in candidate_urls:
            if url in seen:
                continue
            seen.add(url)

            is_progressive = any(token in url.lower() for token in ("account.accountnonmobile.", "librarycheckoutsaccordion", "libraryholdsaccordion"))

            csrf_token = _extract_sdcsrf_from_url(url) or _extract_sdcsrf_from_html(account_html)
            request_url = _strip_sdcsrf_from_url(url) if is_progressive else url
            headers = {
                "Referer": self._account_url,
                "Accept": "text/html, */*; q=0.01",
            }
            if is_progressive:
                headers.update(
                    {
                        "X-Requested-With": "XMLHttpRequest",
                        "X-Prototype-Version": "1.7",
                    }
                )
                if csrf_token:
                    headers["sdcsrf"] = csrf_token

            try:
                page_html = await _request_text(
                    session,
                    "GET",
                    request_url,
                    headers=headers,
                )
            except LinkcatConnectionError:
                continue

            if _looks_logged_out(page_html):
                continue

            if _is_system_error_page(page_html):
                _debug_dump_html(f"{_safe_page_name_from_url(request_url)}_system_error", page_html)
                continue

            pages.append(page_html)
            _debug_dump_html(_safe_page_name_from_url(request_url), page_html)

        return pages

    def _parse_account_data(self, pages: list[str]) -> LinkcatAccountData:
        page_fragments: list[str] = []
        for page_html in pages:
            page_fragments.extend(_extract_html_fragments(page_html))

        body_text = "\n".join(_html_to_text(fragment) for fragment in page_fragments)

        checkouts: list[CheckoutItem] = []
        holds: list[HoldItem] = []

        for page_html in page_fragments:
            if not checkouts:
                checkouts = _extract_checkout_rows(page_html, self._base_url)
            if not holds:
                holds = _extract_hold_rows(page_html, self._base_url)
            if checkouts and holds:
                break

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


_SORT_HEADER_TEXT = "click to sort"
_TITLE_AUTHOR_HEADER_TEXT = "title/author"


def _is_header_row(row_html: str) -> bool:
    """Return True when the row contains no <td> cells (i.e., it is a header/th-only row)."""
    return not bool(re.search(r"<td\b", row_html, re.IGNORECASE))


def _extract_checkout_rows(account_html: str, base_url: str) -> list[CheckoutItem]:
    table_html = _find_table(
        account_html,
        [
            "libraryCheckoutsTable",
            "checkoutsTab",
            "myCheckouts_checkoutslistnonmobile_table",
            "checkoutslistnonmobile_table",
        ],
    )

    checkouts: list[CheckoutItem] = []
    if table_html:
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

    if not checkouts:
        checkouts = _extract_checkout_links_fallback(account_html)

    return _dedupe_checkouts(checkouts)


def _extract_hold_rows(account_html: str, base_url: str) -> list[HoldItem]:
    table_html = _find_table(
        account_html,
        [
            "holdsTab",
            "myHolds_holdslistnonmobile_table",
            "holdslistnonmobile_table",
        ],
    )
    if not table_html:
        return _extract_hold_links_fallback(account_html)

    holds: list[HoldItem] = []
    for row_html in _extract_table_rows(table_html):
        cells = _extract_cells(row_html)
        if len(cells) < 2:
            continue

        row_text = _html_to_text(row_html)
        if _SORT_HEADER_TEXT in row_text.lower() or _is_header_row(row_html):
            continue

        title = None
        author = None
        title_cell_idx = None
        for idx, cell in enumerate(cells):
            anchor = _extract_anchor_text(cell)
            if anchor and _TITLE_AUTHOR_HEADER_TEXT not in anchor.lower():
                title = anchor
                title_cell_idx = idx
                break

        # Fallback: multiline parse across cells
        if not title:
            for idx, cell in enumerate(cells):
                t, a = _parse_title_author_from_multiline_text(_html_to_text(cell))
                if t and _TITLE_AUTHOR_HEADER_TEXT not in t.lower():
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

        if title and _TITLE_AUTHOR_HEADER_TEXT not in title.lower():
            holds.append(HoldItem(title=title, author=author, image_url=image_url, status=status, ready=ready))

    if not holds:
        holds = _extract_hold_links_fallback(account_html)

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
    normalized_html = _normalize_html_fragment(account_html)

    for id_hint in id_hints:
        match = re.search(
            rf"<table[^>]*id=['\"][^'\"]*{re.escape(id_hint)}[^'\"]*['\"][^>]*>(.*?)</table>",
            normalized_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1)

    class_hints = ["checkoutslist", "holdslist", "detailitemtable"]
    for class_hint in class_hints:
        match = re.search(
            rf"<table[^>]*class=['\"][^'\"]*{class_hint}[^'\"]*['\"][^>]*>(.*?)</table>",
            normalized_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1)

    for id_hint in id_hints:
        section_match = re.search(
            rf"<div[^>]*id=['\"][^'\"]*{re.escape(id_hint)}[^'\"]*['\"][^>]*>(.*?)</div>",
            normalized_html,
            flags=re.IGNORECASE | re.DOTALL,
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
    normalized_table = _normalize_html_fragment(table_html)
    return re.findall(r"<tr\b[^>]*>(.*?)</tr>", normalized_table, flags=re.IGNORECASE | re.DOTALL)


def _extract_cells(row_html: str) -> list[str]:
    normalized_row = _normalize_html_fragment(row_html)
    return re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", normalized_row, flags=re.IGNORECASE | re.DOTALL)


def _extract_anchor_text(cell_html: str) -> str | None:
    cell_html = _normalize_html_fragment(cell_html)
    anchor_match = re.search(r"<a\b[^>]*>(.*?)</a>", cell_html, flags=re.IGNORECASE | re.DOTALL)
    if not anchor_match:
        return None
    text = _html_to_text(anchor_match.group(1)).strip()
    return text or None


def _extract_image_url(fragment_html: str, base_url: str) -> str | None:
    fragment_html = _normalize_html_fragment(fragment_html)

    cover_match = re.search(
        r"<img\b[^>]*(?:class=['\"][^'\"]*accountCoverImage[^'\"]*['\"]|id=['\"][^'\"]*(?:checkoutsImage|holdsImage)[^'\"]*['\"])[^>]*\bsrc=['\"]([^'\"]+)['\"]",
        fragment_html,
        flags=re.IGNORECASE,
    )
    if cover_match:
        return urljoin(base_url, html.unescape(cover_match.group(1)).strip())

    image_match = re.search(r"<img\b[^>]*\bsrc=['\"]([^'\"]+)['\"]", fragment_html, flags=re.IGNORECASE)
    if not image_match:
        return None
    return urljoin(base_url, html.unescape(image_match.group(1)).strip())


def _extract_due_date(text: str) -> str | None:
    date_patterns = [
        r"due\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        r"due\s*([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
        r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _extract_checkout_links_fallback(account_html: str) -> list[CheckoutItem]:
    items: list[CheckoutItem] = []
    for title_html in re.findall(
        r"<a[^>]*href=['\"][^'\"]*detailnonmodal[^'\"]*['\"][^>]*>(.*?)</a>",
        account_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        title = _html_to_text(title_html)
        if title and "click to sort" not in title.lower():
            items.append(CheckoutItem(title=title))
    return items


def _extract_hold_links_fallback(account_html: str) -> list[HoldItem]:
    items: list[HoldItem] = []
    for title_html in re.findall(
        r"<a[^>]*href=['\"][^'\"]*detailnonmodal[^'\"]*['\"][^>]*>(.*?)</a>",
        account_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        title = _html_to_text(title_html)
        if title and "click to sort" not in title.lower():
            items.append(HoldItem(title=title))
    return items


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


def _extract_account_links(account_html: str) -> list[str]:
    links: list[str] = []
    for href in re.findall(r"href=['\"]([^'\"]+)['\"]", account_html, flags=re.IGNORECASE):
        href_lower = href.lower()
        if "logout" in href_lower or "clearsession" in href_lower:
            continue
        if "search/account" not in href_lower:
            continue
        if not any(token in href_lower for token in ("checkout", "hold", "dashboard", "account")):
            continue
        links.append(html.unescape(href))
    return links


def _extract_progressive_display_urls(account_html: str) -> list[str]:
    urls: list[str] = []
    for url in re.findall(
        r'"url"\s*:\s*"([^\"]*account\.accountnonmobile\.[^\"]+)"',
        account_html,
        flags=re.IGNORECASE,
    ):
        decoded = html.unescape(url)
        lower = decoded.lower()
        if "librarycheckoutsaccordion" in lower or "libraryholdsaccordion" in lower:
            urls.append(decoded)
    return urls


def _extract_sdcsrf_from_url(url: str) -> str | None:
    match = re.search(r"[?&]sdcsrf=([a-f0-9-]+)", url, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1)


def _extract_sdcsrf_from_html(account_html: str) -> str | None:
    match = re.search(r"var\s+__sdcsrf\s*=\s*\"([a-f0-9-]+)\"", account_html, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1)


def _strip_sdcsrf_from_url(url: str) -> str:
    stripped = re.sub(r"([?&])sdcsrf=[a-f0-9-]+", r"\1", url, flags=re.IGNORECASE)
    stripped = re.sub(r"[?&]{2,}", "&", stripped)
    stripped = stripped.replace("?&", "?")
    if stripped.endswith("?") or stripped.endswith("&"):
        stripped = stripped[:-1]
    return stripped


def _safe_page_name_from_url(url: str) -> str:
    name = re.sub(r"https?://", "", url, flags=re.IGNORECASE)
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    if not name:
        return "page"
    return name[:120]


def _debug_dump_html(name: str, content: str) -> None:
    target_dir = os.getenv("LINKCAT_DEBUG_HTML_DIR", "").strip()
    if not target_dir:
        return

    try:
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, f"{name}.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        _LOGGER.debug("Wrote Linkcat debug HTML to %s", path)
    except Exception:
        _LOGGER.debug("Failed writing Linkcat debug HTML for %s", name, exc_info=True)


def _is_system_error_page(page_html: str) -> bool:
    lowered = page_html.lower()
    return "id=\"exceptionblock\"" in lowered and "system error" in lowered


def _extract_html_fragments(page_html: str) -> list[str]:
    fragments = [_normalize_html_fragment(page_html)]

    stripped = page_html.lstrip()
    if not stripped.startswith("{"):
        return fragments

    try:
        payload = json.loads(page_html)
    except Exception:
        return fragments

    if not isinstance(payload, dict):
        return fragments

    for key in ("content",):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            fragments.append(_normalize_html_fragment(value))

    zones = payload.get("zones")
    if isinstance(zones, dict):
        for zone_html in zones.values():
            if isinstance(zone_html, str) and zone_html.strip():
                fragments.append(_normalize_html_fragment(zone_html))

    return fragments


def _normalize_html_fragment(fragment: str) -> str:
    normalized = html.unescape(fragment)
    normalized = normalized.replace("\\/", "/")
    normalized = normalized.replace("<\\", "<")
    return normalized
