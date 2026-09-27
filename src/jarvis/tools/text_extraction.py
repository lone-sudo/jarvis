"""
Stdlib-only HTML boilerplate stripping. No third-party dependencies --
trafilatura was evaluated and rejected (ADR/build-log pending) for
pulling in lxml (a C-extension) plus courlan/htmldate/justext, which
violates the lean-local-ZBook constraint for what is fundamentally a
"strip script/nav/footer tags" job.

Extraction quality is deliberately lower than trafilatura's heuristics
-- this keeps only the plain text content outside a fixed set of
boilerplate tags, with no attempt at "is this the main article" scoring.
That's an accepted tradeoff for zero dependencies and instant execution.
"""

from html.parser import HTMLParser

# Tags whose entire contents (including nested tags) should be dropped.
_SKIP_TAGS = frozenset({
    "script", "style", "nav", "header", "footer", "noscript", "svg", "form", "button", "aside",
})

# Block-level tags after which we insert a newline, so the output reads
# as paragraphs/lines rather than one run-on line of text.
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "section", "article",
})


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text + " ")

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of blank lines/whitespace left by the block-tag newlines.
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def extract_readable_text(html: str) -> str:
    """
    Strips <script>/<style>/<nav>/<header>/<footer>/etc. and their
    contents, returns the remaining visible text with paragraph breaks
    preserved. Malformed HTML is tolerated -- HTMLParser recovers from
    most real-world mistakes; this never raises on ordinary fetched
    pages.
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.get_text()
