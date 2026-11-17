"""Research helpers for the NorthSignal forecasting bot.

A forecasting question usually names the page its answer will be read from: a statistics
release, a leaderboard, a results page. These helpers find those links in the question text,
fetch the pages, and reduce them to plain text a model can read. Standard library plus httpx only.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

BARE_URL = re.compile(r"https?://[^\s<>\"`]+")
TRAILING_PUNCTUATION = ".,;:!?'\"*>]"

# Pages that cannot be read without a login or a media player, and the question site itself.
SKIPPED_HOSTS = (
    "metaculus.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "t.me",
)
SKIPPED_EXTENSIONS = (
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".zip", ".gz",
    ".xlsx", ".xls", ".docx", ".pptx", ".mp4", ".mp3", ".mov",
)

# An honest, contactable identity: sites such as Wikipedia refuse anonymous-looking bots.
USER_AGENT = "NorthSignalBot/1.0 (forecasting research; +https://github.com/MarcSipflip/northsignal-bot)"
READER_PREFIX = "https://r.jina.ai/"  # renders script-built pages to text; used only as a fallback
MAX_DOWNLOAD_BYTES = 3_000_000
MIN_USEFUL_CHARS = 400


@dataclass(frozen=True)
class SourcePage:
    url: str
    text: str  # empty when the page could not be read


def _clean_url(raw: str) -> str:
    url = raw
    while url and url[-1] in TRAILING_PUNCTUATION:
        url = url[:-1]
    # A closing bracket belongs to the URL only when it has a partner ("Foo_(bar)").
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
        while url and url[-1] in TRAILING_PUNCTUATION:
            url = url[:-1]
    return url


def _is_public_host(host: str) -> bool:
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True  # a normal domain name


def _is_fetchable(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not _is_public_host(host):
        return False
    if any(host == skipped or host.endswith("." + skipped) for skipped in SKIPPED_HOSTS):
        return False
    return not parts.path.lower().endswith(SKIPPED_EXTENSIONS)


def extract_urls(texts: Iterable[str | None], limit: int = 4) -> list[str]:
    """Links found in the given texts, in order, deduplicated. Pass the texts most important first."""
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in BARE_URL.findall(text):
            url = _clean_url(match)
            key = url.split("#", 1)[0].rstrip("/").lower()
            if key in seen or not _is_fetchable(url):
                continue
            seen.add(key)
            found.append(url)
            if len(found) >= limit:
                return found
    return found


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}
    BLOCK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
        "table", "ul", "ol", "blockquote", "pre", "hr", "title", "header", "footer", "dt", "dd",
    }
    CELL = {"td", "th"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        elif self._skip_depth == 0:
            if tag in self.BLOCK:
                self.parts.append("\n")
            elif tag in self.CELL:
                self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif self._skip_depth == 0 and tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # malformed markup: keep whatever was parsed
        logger.debug(f"HTML parse stopped early: {exc}")
    lines = []
    for line in "".join(parser.parts).splitlines():
        line = re.sub(r"[ \t\r\f\v ]+", " ", line).strip()
        line = line.removeprefix("| ").strip()
        if line and line != "|":
            lines.append(line)
    return "\n".join(lines)


def clip(text: str, max_chars: int) -> str:
    """Keep the start and the end: on data pages the newest figures are often at one of the two."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    return text[:head] + "\n[...]\n" + text[-(max_chars - head):]


async def _download(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Returns (content type, body text), with the body capped at MAX_DOWNLOAD_BYTES."""
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_DOWNLOAD_BYTES:
                break
        encoding = response.encoding or "utf-8"
        content_type = response.headers.get("content-type", "").lower()
    try:
        body = b"".join(chunks).decode(encoding, errors="replace")
    except LookupError:
        body = b"".join(chunks).decode("utf-8", errors="replace")
    return content_type, body


async def fetch_page_text(
    client: httpx.AsyncClient, url: str, max_chars: int = 12_000, use_reader_fallback: bool = True
) -> str:
    text = ""
    try:
        content_type, body = await _download(client, url)
        looks_like_html = "html" in content_type or body.lstrip()[:15].lower().startswith(("<!doctype", "<html"))
        if looks_like_html:
            text = html_to_text(body)
        elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type or not content_type:
            text = body
    except (httpx.HTTPError, ValueError) as exc:
        logger.info(f"Could not fetch {url}: {type(exc).__name__}: {exc}")

    if len(text) < MIN_USEFUL_CHARS and use_reader_fallback:
        try:
            _, rendered = await _download(client, READER_PREFIX + url)
            if len(rendered) > len(text):
                text = rendered
        except (httpx.HTTPError, ValueError) as exc:
            logger.info(f"Reader fallback failed for {url}: {type(exc).__name__}: {exc}")

    return clip(text.strip(), max_chars)


async def read_sources(
    urls: list[str], max_chars_per_page: int = 12_000, use_reader_fallback: bool = True
) -> list[SourcePage]:
    if not urls:
        return []
    timeout = httpx.Timeout(25.0, connect=10.0)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        texts = await asyncio.gather(
            *[fetch_page_text(client, url, max_chars_per_page, use_reader_fallback) for url in urls],
            return_exceptions=True,
        )
    pages = []
    for url, text in zip(urls, texts):
        if isinstance(text, BaseException):
            logger.info(f"Reading {url} failed: {type(text).__name__}: {text}")
            text = ""
        pages.append(SourcePage(url=url, text=text))
    return pages
