#!/usr/bin/env python3

"""check_fork_upstream_branding_strings.py

Lightweight guardrail for downstream forks.

When we regularly merge from upstream Zulip, accidental upstream branding
strings can creep back into user-facing templates and frontend sources.
This script scans a curated set of user-facing directories and fails if
it finds forbidden branding strings.

Defaults:
  - Forbid regex: \\bZulip\\b
  - Search paths (relative to the Zulip repo root):
      templates/404.html
      templates/4xx.html
      templates/500.html
      templates/analytics/stats.html
      templates/confirmation
      templates/zerver/base.html
      templates/zerver/footer.html
      templates/zerver/meta_tags.html
      templates/zerver/login.html
      templates/zerver/portico-header.html
      templates/zerver/portico-header-dropdown.html
      templates/zerver/app/index.html
      templates/zerver/portico_error_pages
      templates/zerver/emails
      web/templates
      web/src
      web/html

  - Exclude any paths under **/corporate/** and **/development/**

Rationale:
  We intentionally do not scan all of `templates/zerver/` by default,
  because some templates are rarely/never served in Smarty Pants
  deployments (e.g., corporate marketing flows, dev-only pages). You can
  add additional `--path` entries for a deeper scan.

Usage:
  ./scripts/check_fork_upstream_branding_strings.py
  ./scripts/check_fork_upstream_branding_strings.py --forbid "\\bZulip\\b" --forbid "Zulip Cloud"

Exit status:
  0 if no matches are found; 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_FORBID_PATTERNS: list[str] = [r"\bZulip\b"]
DEFAULT_SEARCH_PATHS: list[str] = [
    # Global error pages.
    "templates/404.html",
    "templates/4xx.html",
    "templates/500.html",

    # Key user-facing pages that should never leak upstream branding.
    "templates/analytics/stats.html",
    "templates/confirmation",
    "templates/zerver/base.html",
    "templates/zerver/footer.html",
    "templates/zerver/meta_tags.html",
    "templates/zerver/login.html",
    "templates/zerver/portico-header.html",
    "templates/zerver/portico-header-dropdown.html",
    "templates/zerver/app/index.html",
    "templates/zerver/portico_error_pages",
    "templates/zerver/emails",

    # Frontend sources.
    "web/templates",
    "web/src",
    "web/html",
]
DEFAULT_EXCLUDED_PATH_COMPONENTS: set[str] = {"corporate", "development"}
DEFAULT_MAX_MATCHES = 200

# We scan a lot of text, but we never want to treat common binary assets as text.
# Allowlist patterns for lines that intentionally include the forbidden
# branding string but are not user-facing (e.g., fallback constants).
ALLOWLIST_LINE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bDEFAULT_BRAND_NAME\b.*\"Zulip\""),
]

# Lines that are clearly comments in JS/TS (not user-facing copy).
COMMENT_LINE_RE = re.compile(r"^\s*(//|/\*|\*|\*/)" )

BINARY_SUFFIXES: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".bz2",
    ".xz",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".webm",
}


@dataclass(frozen=True)
class Match:
    file_path: Path
    line_number: int
    line: str
    pattern: str


def repo_root() -> Path:
    # This script lives in <repo>/scripts; resolve repo root from that.
    return Path(__file__).resolve().parent.parent


def should_exclude_path(path: Path, *, excluded_components: set[str]) -> bool:
    # Exclude if any path component is in the excluded set.
    return any(part in excluded_components for part in path.parts)


def iter_candidate_files(
    root: Path,
    rel_paths: list[str],
    *,
    excluded_components: set[str],
) -> list[Path]:
    files: list[Path] = []
    for rel in rel_paths:
        start = (root / rel).resolve()
        if not start.exists():
            # Keep this script usable across forks that may delete components.
            print(f"warning: missing path {rel}", file=sys.stderr)
            continue

        if start.is_file():
            file_paths = [start]
        else:
            file_paths = list(start.rglob("*"))

        for file_path in file_paths:
            if file_path.is_dir():
                continue
            if file_path.suffix in BINARY_SUFFIXES:
                continue
            if should_exclude_path(file_path.relative_to(root), excluded_components=excluded_components):
                continue
            files.append(file_path)

    # Deterministic output.
    return sorted(set(files))


def scan_file(
    file_path: Path,
    patterns: list[re.Pattern[str]],
    *,
    stop_after: int | None,
) -> list[Match]:
    matches: list[Match] = []
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            is_web_src = "/web/src/" in file_path.as_posix()

            for i, line in enumerate(f, start=1):
                # For `web/src/**`, we only want to guard user-facing translated
                # strings (defaultMessage), not internal comments/logging.
                if is_web_src and "defaultMessage" not in line:
                    continue

                # Skip obvious comment-only lines to avoid false positives.
                if COMMENT_LINE_RE.match(line):
                    continue
                if any(p.search(line) for p in ALLOWLIST_LINE_PATTERNS):
                    continue

                for pattern in patterns:
                    if pattern.search(line):
                        matches.append(
                            Match(
                                file_path=file_path,
                                line_number=i,
                                line=line.rstrip("\n"),
                                pattern=pattern.pattern,
                            )
                        )
                        if stop_after is not None and len(matches) >= stop_after:
                            return matches
    except OSError as e:  # nocoverage - depends on local filesystem state
        print(f"warning: failed reading {file_path}: {e}", file=sys.stderr)
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan user-facing files for forbidden upstream branding strings.",
    )
    parser.add_argument(
        "--forbid",
        dest="forbid",
        action="append",
        default=list(DEFAULT_FORBID_PATTERNS),
        help=(
            "Regex pattern to forbid. Can be provided multiple times. "
            f"(default: {DEFAULT_FORBID_PATTERNS!r})"
        ),
    )
    parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        default=list(DEFAULT_SEARCH_PATHS),
        help=(
            "Relative path to scan. Can be provided multiple times. "
            f"(default: {DEFAULT_SEARCH_PATHS!r})"
        ),
    )
    parser.add_argument(
        "--exclude-component",
        dest="exclude_components",
        action="append",
        default=sorted(DEFAULT_EXCLUDED_PATH_COMPONENTS),
        help=(
            "Exclude any file with this path component (e.g. 'corporate'). "
            "Can be provided multiple times."
        ),
    )
    parser.add_argument(
        "--max-matches",
        type=int,
        default=DEFAULT_MAX_MATCHES,
        help=(
            "Stop after reporting this many matches (0 means unlimited). "
            f"Default: {DEFAULT_MAX_MATCHES}."
        ),
    )

    args = parser.parse_args()

    root = repo_root()
    compiled_patterns = [re.compile(p) for p in args.forbid]
    excluded_components = set(args.exclude_components)

    files = iter_candidate_files(root, args.paths, excluded_components=excluded_components)

    max_matches: int | None
    if args.max_matches == 0:
        max_matches = None
    elif args.max_matches < 0:
        raise SystemExit("--max-matches must be >= 0")
    else:
        max_matches = args.max_matches

    all_matches: list[Match] = []
    for file_path in files:
        remaining = None if max_matches is None else max_matches - len(all_matches)
        if remaining is not None and remaining <= 0:
            break
        all_matches.extend(scan_file(file_path, compiled_patterns, stop_after=remaining))

    if not all_matches:
        return 0

    print("Forbidden branding strings found:\n", file=sys.stderr)
    for m in all_matches:
        rel = m.file_path.relative_to(root)
        print(f"{rel}:{m.line_number}: [{m.pattern}] {m.line}", file=sys.stderr)

    if max_matches is not None:
        print(
            f"\nStopped after {max_matches} matches. Re-run with --max-matches 0 to show all.",
            file=sys.stderr,
        )

    print(
        "\nTo update the forbidden patterns or exclusions, see the script header.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
