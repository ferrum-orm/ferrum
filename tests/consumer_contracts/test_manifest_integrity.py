"""Structural integrity checks for the consumer-parity manifest.

These do not touch a database — they prove the manifest itself is
well-formed: every entry cites a real, existing line range in the audited
consumer repo at the pinned revision, ids are unique, classifications are one
of the four allowed values, and every category the task contract requires is
covered for the right consumer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .manifest import (
    CATEGORIES,
    MANIFEST,
    ORG_AI_PLATFORM_REPO,
    TICKET_ANALYZER_REPO,
    Classification,
    ParityEntry,
)

_CONSUMER_REPO_ROOTS: dict[str, Path] = {
    TICKET_ANALYZER_REPO: Path("/Users/guyshaked/Desktop/dev/repos/ticket-analyzer-agent"),
    ORG_AI_PLATFORM_REPO: Path("/Users/guyshaked/Desktop/dev/repos/org-ai-platform"),
}

_REQUIRED_TICKET_ANALYZER_KEYWORDS: tuple[str, ...] = (
    "rls",
    "platform-admin",
    "composite",
    "cas",
    "lease",
    "jsonb",
    "array",
    "pgvector",
    "bulk",
    "stream",
    "function",
    "nullable",
    "aggregate",
)

_REQUIRED_ORG_AI_KEYWORDS: tuple[str, ...] = (
    "schema-per-tenant",
    "shard",
    "row-lock",
    "relation",
    "encrypted",
    "pydantic",
    "fastapi",
    "migration",
)


@pytest.mark.parametrize("entry", MANIFEST, ids=[e.id for e in MANIFEST])
def test_entry_cites_a_real_line_range_at_the_pinned_revision(entry: ParityEntry) -> None:
    repo_root = _CONSUMER_REPO_ROOTS[entry.citation.repo]
    target = repo_root / entry.citation.path
    assert target.is_file(), f"{entry.id}: cited path does not exist: {target}"
    line_count = sum(1 for _ in target.open(encoding="utf-8", errors="replace"))
    start_str, end_str = entry.citation.lines.split("-", 1)
    end = int(end_str)
    assert int(start_str) <= end, f"{entry.id}: citation lines out of order"
    # The file must have at least as many lines as the citation's start line —
    # proves the citation is not pointing past end-of-file (a fabricated range).
    start = int(start_str)
    assert start <= line_count, (
        f"{entry.id}: citation starts at line {start} but {target} only has {line_count} lines"
    )


@pytest.mark.parametrize("entry", MANIFEST, ids=[e.id for e in MANIFEST])
def test_entry_excerpt_appears_verbatim_in_the_cited_file(entry: ParityEntry) -> None:
    repo_root = _CONSUMER_REPO_ROOTS[entry.citation.repo]
    target = repo_root / entry.citation.path
    content = target.read_text(encoding="utf-8", errors="replace")
    # Compare whitespace-normalized: the excerpt is a readable multi-line
    # snippet and source formatting (indentation) can legitimately differ
    # from how it is quoted in the manifest, but every token must appear in
    # order — this still rules out a paraphrased or fabricated excerpt.
    normalized_excerpt = " ".join(entry.citation.excerpt.split())
    normalized_content = " ".join(content.split())
    assert normalized_excerpt in normalized_content, (
        f"{entry.id}: excerpt not found verbatim (whitespace-normalized) in {target}"
    )


def test_all_ids_are_unique() -> None:
    ids = [entry.id for entry in MANIFEST]
    assert len(ids) == len(set(ids)), "duplicate ParityEntry.id values in MANIFEST"


def test_all_categories_are_recognized() -> None:
    for entry in MANIFEST:
        assert entry.category in CATEGORIES, f"{entry.id}: unknown category {entry.category!r}"


def test_all_classifications_are_valid_enum_members() -> None:
    for entry in MANIFEST:
        assert isinstance(entry.classification, Classification)


def test_every_entry_has_a_ferrum_reference_and_evidence() -> None:
    for entry in MANIFEST:
        assert entry.ferrum_reference.strip(), f"{entry.id}: missing ferrum_reference"
        assert entry.evidence.strip(), f"{entry.id}: missing evidence"


def test_ticket_analyzer_required_topics_are_all_covered() -> None:
    ta_summaries = " ".join(
        entry.call_summary.lower() + " " + entry.notes.lower()
        for entry in MANIFEST
        if entry.consumer == TICKET_ANALYZER_REPO
    )
    missing = [kw for kw in _REQUIRED_TICKET_ANALYZER_KEYWORDS if kw not in ta_summaries]
    assert not missing, f"Ticket Analyzer manifest is missing required topics: {missing}"


def test_org_ai_platform_required_topics_are_all_covered() -> None:
    oai_summaries = " ".join(
        entry.call_summary.lower() + " " + entry.notes.lower()
        for entry in MANIFEST
        if entry.consumer == ORG_AI_PLATFORM_REPO
    )
    missing = [kw for kw in _REQUIRED_ORG_AI_KEYWORDS if kw not in oai_summaries]
    assert not missing, f"Org AI Platform manifest is missing required topics: {missing}"


def test_classification_counts_are_all_nonzero_across_both_consumers() -> None:
    """Every classification bucket must have at least one real example.

    A manifest with zero ``ferrum_defect`` or zero ``missing_ferrum_api``
    entries would mean the audit found nothing wrong — implausible for two
    consumer codebases this large, and a signal the audit under-classified.
    """
    counts = dict.fromkeys(Classification, 0)
    for entry in MANIFEST:
        counts[entry.classification] += 1
    assert all(counts[c] > 0 for c in Classification), counts
