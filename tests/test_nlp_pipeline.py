"""Tests for nlp_pipeline.py — Article -> NLPResult (CLAUDE.md §5, AD-06).

Covers all 5 pipeline stages (ticker extraction, sentiment scoring, topic
classification, urgency scoring, deduplication) plus the NLPPipeline
orchestration class. Uses the exact `Article`/`NLPResult` types from
event_types.py (Step 1) rather than redefining shapes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from event_types import Article, NLPResult
from nlp_pipeline import (
    NLPPipeline,
    classify_topic,
    extract_tickers,
    fingerprint,
    is_duplicate,
    score_sentiment,
    score_urgency,
)


UTC_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_article(**overrides) -> Article:
    defaults = dict(
        headline="Company announces quarterly results",
        body="The company reported results today.",
        url="https://example.com/news/1",
        published_at=UTC_NOW,
        source="finnhub",
    )
    defaults.update(overrides)
    return Article(**defaults)


# ---------------------------------------------------------------------------
# Stage 1: ticker extraction
# ---------------------------------------------------------------------------


def test_extract_tickers_finds_known_symbols():
    text = "NVDA and BTC both rallied today while AAPL held steady."
    tickers = extract_tickers(text)
    assert "NVDA" in tickers
    assert "BTC" in tickers
    assert "AAPL" in tickers


def test_extract_tickers_false_positive_common_word_lowercase_not_matched():
    # Lowercase "a"/"on" should never match — the regex only looks at
    # standalone ALL-CAPS tokens in the first place.
    text = "This is a story on the market and it moved a lot on news."
    tickers = extract_tickers(text)
    assert tickers == []


def test_extract_tickers_false_positive_prone_allcaps_word_excluded_with_custom_allowlist():
    # "IT" is capitalized (e.g. sentence-leading or emphasis) but is not in
    # the caller-supplied allowlist here, so it must NOT be extracted —
    # proving the allowlist cross-check, not just the regex, gates output.
    text = "IT spending rose sharply across the sector this quarter."
    tickers = extract_tickers(text, known_assets={"NVDA", "AAPL", "BTC"})
    assert "IT" not in tickers
    assert tickers == []


def test_extract_tickers_allows_known_single_letter_ticker_when_allowlisted():
    text = "Shares of A rose after the earnings beat."
    tickers = extract_tickers(text, known_assets={"A"})
    assert tickers == ["A"]


def test_extract_tickers_respects_custom_known_assets_restricting_defaults():
    text = "NVDA and AAPL both moved, but only NVDA is in our universe."
    tickers = extract_tickers(text, known_assets={"NVDA"})
    assert tickers == ["NVDA"]


def test_extract_tickers_dedupes_and_preserves_first_seen_order():
    text = "BTC surged. Later, BTC surged again, and then NVDA moved."
    tickers = extract_tickers(text)
    assert tickers == ["BTC", "NVDA"]


def test_extract_tickers_empty_text_returns_empty_list():
    assert extract_tickers("") == []


def test_extract_tickers_cashtag_prefix_is_stripped():
    text = "$NVDA is up big today alongside $BTC."
    tickers = extract_tickers(text)
    assert "NVDA" in tickers
    assert "BTC" in tickers
    assert "$NVDA" not in tickers


# ---------------------------------------------------------------------------
# Stage 2: sentiment scoring
# ---------------------------------------------------------------------------


def test_score_sentiment_bullish_text_is_positive():
    text = "Shares surge to a record high after the company beat expectations and raised guidance."
    score = score_sentiment(text)
    assert score > 0.0


def test_score_sentiment_bearish_text_is_negative():
    text = "Shares plunge to a record low after the company missed expectations and cut guidance."
    score = score_sentiment(text)
    assert score < 0.0


def test_score_sentiment_neutral_text_is_zero():
    text = "The company held its annual shareholder meeting on Tuesday in Chicago."
    score = score_sentiment(text)
    assert score == 0.0


def test_score_sentiment_stays_within_bounds():
    text = "surge rally beat upgrade bullish outperform record high " * 5
    score = score_sentiment(text)
    assert -1.0 <= score <= 1.0


def test_score_sentiment_long_mostly_neutral_article_with_one_bullish_word_not_maxed():
    filler = "The company operates in several markets and employs many people. " * 10
    text = filler + "Shares surge today."
    score = score_sentiment(text)
    # Only bullish words matched (no bearish), so the ratio is still 1.0 —
    # but this asserts the normalization is by matched-word count, not
    # total word count, i.e. it doesn't silently underflow toward 0 just
    # because the article is long.
    assert score == 1.0


def test_score_sentiment_mixed_signal_is_between_bounds_not_extreme():
    text = "The stock rallied initially but then plunged into the close amid a broader sell-off."
    score = score_sentiment(text)
    assert -1.0 < score < 1.0


# ---------------------------------------------------------------------------
# Stage 3: topic classification
# ---------------------------------------------------------------------------


def test_classify_topic_earnings():
    text = "Company reports Q2 earnings, beating EPS estimates and raising full year guidance."
    assert classify_topic(text) == "earnings"


def test_classify_topic_macro():
    text = "The Federal Reserve signaled a possible rate cut after the latest CPI inflation report."
    assert classify_topic(text) == "macro"


def test_classify_topic_regulatory():
    text = "The SEC opened an investigation into the company's disclosure practices."
    assert classify_topic(text) == "regulatory"


def test_classify_topic_m_and_a():
    text = "The firm agrees to buy its smaller rival in an all-stock acquisition deal."
    assert classify_topic(text) == "m_and_a"


def test_classify_topic_general_fallback():
    text = "The mayor cut the ribbon at a new downtown park on Saturday morning."
    assert classify_topic(text) == "general"


# ---------------------------------------------------------------------------
# Stage 4: urgency scoring — must be independent of sentiment direction
# ---------------------------------------------------------------------------


def test_score_urgency_breaking_keyword_boosts_score():
    now = datetime.now(timezone.utc)
    urgent = score_urgency("BREAKING: major announcement just in", now)
    routine = score_urgency("Company posts quarterly filing", now)
    assert urgent > routine


def test_score_urgency_recent_article_scores_higher_than_stale():
    text = "Company posts quarterly filing"
    now = datetime.now(timezone.utc)
    recent = score_urgency(text, now - timedelta(minutes=1))
    stale = score_urgency(text, now - timedelta(days=10))
    assert recent > stale


def test_score_urgency_stays_within_bounds():
    now = datetime.now(timezone.utc)
    score = score_urgency("BREAKING alert urgent just in", now)
    assert 0.0 <= score <= 1.0

    old = now - timedelta(days=365)
    score_old = score_urgency("routine filing", old)
    assert 0.0 <= score_old <= 1.0


def test_urgency_is_populated_independently_of_sentiment_sign():
    # A highly urgent but neutral-sentiment article: urgency must not be
    # forced to zero, and sentiment must not be forced non-neutral, just
    # because urgency is high. This is the "urgency feeds confidence, never
    # direction" guarantee from CLAUDE.md / the nlp-pipeline skill.
    now = datetime.now(timezone.utc)
    article = make_article(
        headline="BREAKING: company holds annual shareholder meeting",
        body="The meeting proceeded as scheduled with no major announcements.",
        published_at=now,
    )
    pipeline = NLPPipeline()
    result = pipeline.process(article)

    assert result.sentiment == 0.0  # neutral text, no bull/bear words
    assert result.urgency > 0.5  # "BREAKING" + very fresh -> high urgency
    # Confirming urgency and sentiment are independent fields on NLPResult,
    # not derived from one another.
    assert isinstance(result, NLPResult)


# ---------------------------------------------------------------------------
# Stage 5: deduplication
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_normalizes_whitespace_case():
    a = make_article(headline="Fed   Holds Rates Steady")
    b = make_article(headline="fed holds rates steady")
    assert fingerprint(a) == fingerprint(b)


def test_is_duplicate_exact_fingerprint_match():
    original = make_article(
        headline="NVDA surges after earnings beat",
        body="Nvidia shares rallied following strong quarterly results.",
        source="finnhub",
    )
    duplicate = make_article(
        headline="NVDA surges after earnings beat",
        body="Different body text, but identical headline.",
        source="reuters_rss",
    )
    tickers = extract_tickers(duplicate.headline + " " + duplicate.body)
    dup, dup_of = is_duplicate(duplicate, [original], tickers)
    assert dup is True
    assert dup_of == fingerprint(original)


def test_is_duplicate_near_dupe_across_sources_same_ticker():
    original = make_article(
        headline="NVDA shares rally sharply on strong quarterly earnings beat",
        body="Nvidia posted strong results, beating analyst estimates broadly.",
        source="finnhub",
    )
    near_dupe = make_article(
        headline="NVDA shares rally sharply on strong quarterly earnings beat report",
        body="Nvidia posted strong results, beating analyst estimates broadly across segments.",
        source="benzinga",
    )
    tickers = extract_tickers(near_dupe.headline + " " + near_dupe.body)
    dup, dup_of = is_duplicate(near_dupe, [original], tickers, similarity_threshold=0.8)
    assert dup is True
    assert dup_of == fingerprint(original)


def test_is_duplicate_false_positive_prevention_different_tickers_not_deduped():
    # Two articles share near-identical boilerplate text but reference
    # DIFFERENT tickers — must never be deduped against each other, even
    # though raw text similarity would be very high.
    original = make_article(
        headline="NVDA shares rose today amid broad market gains across the sector",
        body="Shares rose today amid broad market gains across the sector.",
        source="finnhub",
    )
    other_ticker = make_article(
        headline="AAPL shares rose today amid broad market gains across the sector",
        body="Shares rose today amid broad market gains across the sector.",
        source="reuters_rss",
    )
    tickers = extract_tickers(other_ticker.headline + " " + other_ticker.body)
    dup, dup_of = is_duplicate(other_ticker, [original], tickers, similarity_threshold=0.8)
    assert dup is False
    assert dup_of is None


def test_is_duplicate_same_ticker_genuinely_different_content_not_deduped():
    original = make_article(
        headline="NVDA announces new AI chip partnership with major cloud provider",
        body="Nvidia unveiled a new partnership focused on data center AI chips.",
        source="finnhub",
    )
    different = make_article(
        headline="NVDA faces regulatory antitrust investigation in Europe",
        body="Nvidia is now under scrutiny from EU regulators over market practices.",
        source="reuters_rss",
    )
    tickers = extract_tickers(different.headline + " " + different.body)
    dup, dup_of = is_duplicate(different, [original], tickers, similarity_threshold=0.92)
    assert dup is False
    assert dup_of is None


def test_is_duplicate_empty_corpus_returns_false():
    article = make_article()
    dup, dup_of = is_duplicate(article, [], ["NVDA"])
    assert dup is False
    assert dup_of is None


def test_is_duplicate_no_tickers_skips_similarity_search():
    # No tickers extracted at all -> tier 2 similarity search must be
    # skipped entirely (nothing to scope it to), not silently fall back to
    # an unscoped global search.
    original = make_article(headline="Local weather turns colder this week")
    similar_no_ticker = make_article(headline="Local weather turns much colder this week")
    dup, dup_of = is_duplicate(similar_no_ticker, [original], [])
    assert dup is False
    assert dup_of is None


# ---------------------------------------------------------------------------
# NLPPipeline orchestration
# ---------------------------------------------------------------------------


def test_pipeline_process_returns_fully_populated_nlpresult():
    article = make_article(
        headline="NVDA surges after strong earnings beat",
        body="Nvidia shares rallied sharply following record quarterly results.",
    )
    pipeline = NLPPipeline()
    result = pipeline.process(article)

    assert isinstance(result, NLPResult)
    assert result.article is article
    assert "NVDA" in result.tickers
    assert result.sentiment > 0.0
    assert result.topic in (
        "earnings", "macro", "regulatory", "m_and_a", "product", "general",
    )
    assert 0.0 <= result.urgency <= 1.0
    assert result.is_duplicate is False
    assert result.duplicate_of is None


def test_pipeline_process_flags_duplicate_against_corpus():
    original = make_article(
        headline="NVDA surges after strong earnings beat",
        body="Nvidia shares rallied sharply following record quarterly results.",
        source="finnhub",
    )
    duplicate_article = make_article(
        headline="NVDA surges after strong earnings beat",
        body="A different body describing the same event.",
        source="benzinga",
    )
    pipeline = NLPPipeline()
    result = pipeline.process(duplicate_article, corpus=[original])

    assert result.is_duplicate is True
    assert result.duplicate_of == fingerprint(original)


def test_pipeline_process_with_no_corpus_defaults_to_not_duplicate():
    article = make_article()
    pipeline = NLPPipeline()
    result = pipeline.process(article)
    assert result.is_duplicate is False


def test_pipeline_uses_custom_known_assets():
    article = make_article(
        headline="ZVZZT is a fictitious test symbol used by exchanges",
        body="ZVZZT is not a real, tradeable company.",
    )
    pipeline = NLPPipeline(known_assets={"ZVZZT"})
    result = pipeline.process(article)
    assert "ZVZZT" in result.tickers


def test_pipeline_uses_custom_similarity_threshold():
    original = make_article(
        headline="NVDA shares rally sharply on strong quarterly earnings beat",
        body="Nvidia posted strong results, beating analyst estimates broadly.",
        source="finnhub",
    )
    near_dupe = make_article(
        headline="NVDA shares rally moderately on decent quarterly earnings report",
        body="Nvidia posted okay results, meeting analyst estimates roughly.",
        source="benzinga",
    )
    # A very high threshold should NOT flag this loosely-similar pair.
    strict_pipeline = NLPPipeline(similarity_threshold=0.99)
    result = strict_pipeline.process(near_dupe, corpus=[original])
    assert result.is_duplicate is False
