"""
Run this on the ZBook to see exactly what the stdlib extractor produces
from your REAL captured raw HTML (item INB-3A322D98, the Wikipedia
fetch from the V3 round) -- not a synthetic sandbox sample.

Usage:
    python compare_extraction.py <path-to-inbox-item.md>

Since the inbox item's Markdown file already has the raw fetched HTML
stored under "## Original Capture", this reads that section directly.
"""

import sys
from pathlib import Path

sys.path.insert(0, "src")
from jarvis.tools.text_extraction import extract_readable_text


def main():
    if len(sys.argv) != 2:
        print("Usage: python compare_extraction.py <path-to-INB-xxxxxxxx.md>")
        sys.exit(1)

    md_path = Path(sys.argv[1])
    text = md_path.read_text(encoding="utf-8")
    marker = "## Original Capture\n\n"
    if marker not in text:
        print("ERROR: couldn't find '## Original Capture' section in this file.")
        sys.exit(1)

    raw_html = text.split(marker, 1)[1].split("\n## User's Note", 1)[0].strip()

    print(f"Raw captured content: {len(raw_html)} chars")
    print("-" * 60)
    print(raw_html[:300])
    print("...\n")

    extracted = extract_readable_text(raw_html)
    print(f"Extracted text: {len(extracted)} chars ({100 * len(extracted) // max(len(raw_html), 1)}% of original)")
    print("-" * 60)
    print(extracted[:1000])
    print("..." if len(extracted) > 1000 else "")
    print()
    print("Report back: does the extracted text look like real article content,")
    print("or is there leftover navigation/boilerplate that slipped through?")


if __name__ == "__main__":
    main()
