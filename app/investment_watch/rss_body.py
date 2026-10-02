"""Reuse explicit RSS content; a description is never promoted to full text."""
from bs4 import BeautifulSoup
from intelligence.adapters import _safe_xml_root

MAX_FEED_CONTENT = 300000


def content_by_id(body, base_url):
    from intelligence.adapters import _children, _first_text, _entry_link, _local_name
    root = _safe_xml_root(body)
    kind = _local_name(root.tag)
    channels = _children(root, "channel")
    entries = _children(root, "entry" if kind == "feed" else "item")
    if kind == "rss" and channels:
        entries = _children(channels[0], "item")
    result = {}
    for entry in entries[:50]:
        identity = _first_text(entry, "guid", "id") or _entry_link(entry, base_url)
        # RSS content:encoded / Atom content, excluding externally linked content.
        raw = ""
        for child in entry:
            if (child.tag == "{http://purl.org/rss/1.0/modules/content/}encoded"
                    or kind == "feed" and _local_name(child.tag) == "content" and not child.get("src")):
                from xml.etree import ElementTree
                raw = (child.text or "") + "".join(ElementTree.tostring(n, encoding="unicode") for n in child)
                break
        if raw and len(raw) <= MAX_FEED_CONTENT:
            result[identity[:300]] = raw
    return result


def save_feed_body(version, raw):
    if not isinstance(raw, str) or len(raw) > MAX_FEED_CONTENT:
        return None
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup.select("script, style, nav, iframe"):
        node.decompose()
    text = soup.get_text("\n", strip=True).strip()
    # Explicit content can still be a teaser. Keep uncertain short/truncated text
    # as metadata and allow the ordinary capture route to obtain the article.
    if len(text) < 250 or any(w in text.casefold() for w in (
            "continue reading", "read more", "subscribe to continue", "阅读更多", "阅读全文", "订阅后阅读")):
        return None
    from .body_capture import _snapshot
    return _snapshot(version, text, raw.encode(), version.url, "rss-content")
