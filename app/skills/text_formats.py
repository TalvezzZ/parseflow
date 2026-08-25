"""Shared suffix definitions for text parsing, upload defaults, and planning."""

STRUCTURED_TEXT_SUFFIXES = frozenset({
    ".csv",
    ".tsv",
    ".html",
    ".htm",
    ".xml",
    ".rtf",
})

RAW_SOURCE_SUFFIXES = frozenset({
    ".json",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".conf",
    ".log",
    ".sql",
    ".js",
    ".ts",
    ".css",
})

PLAIN_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown"})
TEXT_SUFFIXES = STRUCTURED_TEXT_SUFFIXES | RAW_SOURCE_SUFFIXES | PLAIN_TEXT_SUFFIXES
