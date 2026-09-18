#!/usr/bin/env bash
# Extract a specific release section from CHANGELOG.md into an output file.
#
# Usage:
#   .github/scripts/extract-release-notes.sh <version> <changelog-path> <out-path>
#
# Deliberate design:
#   The strict regex intentionally does NOT match historical headers that carry
#   a compare link (e.g. `## [0.1.37](https://...) - date`). Extraction only
#   works for the towncrier format (`## [X.Y.Z] - YYYY-MM-DD`), which protects
#   against tagging without a release-prep PR having compiled the fragments.

set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 <version> <changelog-path> <out-path>" >&2
  exit 1
fi

VERSION="$1"
CHANGELOG="$2"
OUT_PATH="$3"

if [[ ! -f "$CHANGELOG" ]]; then
  echo "Error: changelog file '$CHANGELOG' not found or is not a regular file." >&2
  exit 1
fi

# Normalize CRLF once so matching, line numbers, and extraction use the same
# input. Escape every extended-regex metacharacter in the version literal.
NORMALIZED_CHANGELOG=$(mktemp)
trap 'rm -f "$NORMALIZED_CHANGELOG"' EXIT
tr -d '\r' < "$CHANGELOG" > "$NORMALIZED_CHANGELOG"
VERSION_RE=$(printf '%s' "$VERSION" | sed 's/[][\\.^$*+?{}()|]/\\&/g')

# Verify that exactly ONE towncrier insertion marker exists in CHANGELOG.md.
MARKER="<!-- towncrier release notes start -->"
MARKER_COUNT=$(grep -cFx -- "$MARKER" "$NORMALIZED_CHANGELOG" || true)
if [[ "$MARKER_COUNT" -ne 1 ]]; then
  echo "Error: expected exactly one '$MARKER' marker in '$CHANGELOG' (found $MARKER_COUNT)." >&2
  exit 1
fi
MARKER_LINE=$(grep -nFx -- "$MARKER" "$NORMALIZED_CHANGELOG" | cut -d: -f1)

# Strict towncrier release header pattern: ## [X.Y.Z] - YYYY-MM-DD
# This intentionally excludes legacy headers containing compare links (e.g., `## [0.1.37](...) - ...`).
HEADER_PATTERN="^## \[${VERSION_RE}\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$"

# Verify that exactly ONE section matches the given version.
MATCH_COUNT=$(grep -cE "$HEADER_PATTERN" "$NORMALIZED_CHANGELOG" || true)

if [[ "$MATCH_COUNT" -eq 0 ]]; then
  echo "Error: section not found for version '$VERSION' in '$CHANGELOG' (section not found or ambiguous; expected format: '## [${VERSION}] - YYYY-MM-DD')." >&2
  exit 1
elif [[ "$MATCH_COUNT" -gt 1 ]]; then
  echo "Error: release section for version '$VERSION' is ambiguous in '$CHANGELOG' ($MATCH_COUNT matching sections found)." >&2
  exit 1
fi

# Ensure output directory exists if specified.
OUT_DIR="$(dirname "$OUT_PATH")"
if [[ -n "$OUT_DIR" && ! -d "$OUT_DIR" ]]; then
  mkdir -p "$OUT_DIR"
fi

# Locate the already-validated header, then extract through the line before any
# next level-two heading. Keeping the regex out of awk avoids a second parser.
HEADER_LINE=$(grep -nE "$HEADER_PATTERN" "$NORMALIZED_CHANGELOG" | cut -d: -f1)
if [[ "$HEADER_LINE" -le "$MARKER_LINE" ]]; then
  echo "Error: release section for version '$VERSION' (line $HEADER_LINE) must appear after the towncrier marker (line $MARKER_LINE) in '$CHANGELOG'." >&2
  exit 1
fi

awk -v start="$HEADER_LINE" 'NR>=start { if (NR>start && /^## /) exit; print }' \
  "$NORMALIZED_CHANGELOG" > "$OUT_PATH"

LINE_COUNT=$(wc -l < "$OUT_PATH" | tr -d '[:space:]')

if [[ "$LINE_COUNT" -le 2 ]]; then
  echo "Error: extracted release notes for version '$VERSION' are empty or too small ($LINE_COUNT lines, minimum expected > 2)." >&2
  exit 1
fi

CONTENT_LINE_COUNT=$(awk 'NR > 1 && NF && $0 !~ /^(#| #|  #|   #)/ { count++ } END { print count + 0 }' "$OUT_PATH")
if [[ "$CONTENT_LINE_COUNT" -eq 0 ]]; then
  echo "Error: extracted release notes for version '$VERSION' contain no non-empty content after the header." >&2
  exit 1
fi

echo "Extracted $LINE_COUNT lines of release notes for version $VERSION to $OUT_PATH" >&2
