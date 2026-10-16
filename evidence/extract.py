"""
evidence/extract.py — bounded text extraction from fetched documents (stdlib only, no network).

html_to_text: strips script / style / nav noise, keeps headings and paragraphs as lines.
extract_title / extract_date: <title> or og:title; article:published_time / date / pubdate meta or the first ISO / long date in
the text. snippet: the first N characters of normalised text. Everything is deterministic and never guesses a date: when no
date is found, None is returned and the caller records that the evidence date fell back to the retrieval date.
"""
from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from typing import Optional, Tuple

_SKIP = {"script", "style", "noscript", "svg", "head", "iframe", "template"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol", "header", "footer", "pre"}
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                                        "november", "december"), start=1)}
_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})")
_LONG = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})\b")
_WS = re.compile(r"[ \t\r\f\v]+")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self.title: list = []
        self.meta: dict = {}
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
            return
        if not self._skip and data.strip():
            self.parts.append(data)


def parse_html(html: str) -> Tuple[str, str, dict]:
    """(text, title, meta) — text with block boundaries as newlines, whitespace normalised."""
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup still yields whatever was parsed
        pass
    text = "".join(p.parts)
    lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    title = _WS.sub(" ", " ".join(p.title)).strip() or p.meta.get("og:title", "")
    return text, title, p.meta


def html_to_text(html: str) -> str:
    return parse_html(html)[0]


def extract_date(meta: dict, text: str) -> Optional[date]:
    """Meta tags first, then the first ISO or long-form date in the text. None when nothing parses."""
    for key in ("article:published_time", "date", "pubdate", "publishdate", "datepublished", "dc.date", "og:published_time", "last-modified"):
        v = meta.get(key)
        if v:
            d = _parse_any(v)
            if d:
                return d
    return _parse_any(text[:4000])


def _parse_any(s: str) -> Optional[date]:
    m = _ISO.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _LONG.search(s)
    if m:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        except (ValueError, KeyError):
            pass
    return None


def snippet(text: str, n: int = 2000) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"
