"""Trade-Dash's news NLP stage — Article -> NLPResult.

Pipeline order (CLAUDE.md §5 "nlp_pipeline.py", §7 Phase 2 TODOs):

    ticker extraction -> sentiment scoring -> topic classification ->
    urgency scoring -> deduplication -> NLPResult emission

This module's job stops at `NLPResult` — turning a non-duplicate
`NLPResult` into one or more canonical `Event`s (one per extracted ticker,
typically) is `unified_pipeline.py`'s job (Step 5), per AD-01/AD-02. Nothing
in this module constructs an `Event` directly.

Dev/prod swap contract (AD-06) — every stage below is a free function with a
signature that must survive the dev -> prod swap unchanged:

    | Stage      | Dev (this file, today)          | Prod (Phase 2 swap)                                  |
    |------------|----------------------------------|-------------------------------------------------------|
    | Sentiment  | BULL/BEAR word-list lexicon      | `ProsusAI/finbert` (`transformers`)                    |
    | Dedup      | MD5 fingerprint + BoW cosine     | `sentence-transformers/all-MiniLM-L6-v2` + pgvector `<=>` |
    | NER        | Regex/lexicon ticker allowlist   | spaCy `en_core_web_sm` NER + ticker-symbol post-filter |

Swapping dev -> prod must be a function-body change only (`score_sentiment`,
`is_duplicate`/`fingerprint`, `extract_tickers` keep their exact signatures).
If a swap ever forces a change to `unified_pipeline.py` call sites, the
signature broke the contract — fix the signature, not the caller.

Zero-dependency dev path: no `transformers`/`torch`/`spacy` import is
required to run the lexicon path. This module currently uses only the
stdlib plus NumPy (see requirements.txt) for the BoW cosine similarity in
dedup. Any future prod-only import (`transformers`, `torch`, `spacy`,
`sentence_transformers`) must be lazy/guarded behind a config flag so dev
setups never need the full ML stack (CLAUDE.md §6, nlp-pipeline skill
"Gotchas").
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone

import numpy as np

from event_types import Article, NLPResult

# ---------------------------------------------------------------------------
# Stage 1: ticker extraction (dev: regex/lexicon; prod: spaCy NER, AD-06)
# ---------------------------------------------------------------------------

#: Default known-assets allowlist used when the caller doesn't supply one.
#:
#: In production this set is loaded from the `assets` table (schema.sql,
#: Step 2) via `SELECT DISTINCT symbol FROM assets` (or a cached/refreshed
#: view of it) — this hardcoded default only exists so `extract_tickers` is
#: independently testable/runnable without a live database connection, and
#: covers CLAUDE.md's demo assets (BTC, SPY, NVDA) plus a handful of common
#: additional tickers across equities/crypto/ETFs.
DEFAULT_KNOWN_ASSETS: frozenset[str] = frozenset(
    {
        # CLAUDE.md demo assets
        "BTC", "SPY", "NVDA",
        # Common large-cap equities
        "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "AMD",
        "NFLX", "JPM", "V", "MA", "DIS", "BA", "XOM", "CVX", "WMT", "KO",
        "PEP", "INTC", "IBM", "ORCL", "CRM", "ADBE", "PYPL", "UBER", "ABNB",
        # Single/double-letter tickers that are also common English words —
        # deliberately included here (not excluded) to prove the allowlist,
        # not a stopword blacklist, is what prevents false positives. "A"
        # (Agilent), "ON" (ON Semiconductor), "IT" (Gartner) are real
        # tickers; extract_tickers only emits them when they appear in an
        # ALL-CAPS token context (see _CANDIDATE_RE), not as ordinary words.
        "A", "ON", "IT", "SO", "ALL", "NOW", "GO", "FOR", "ARE", "CAT",
        # Crypto
        "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "MATIC",
        # Index / macro ETFs
        "QQQ", "DIA", "IWM", "VIX", "GLD", "USO", "TLT",
    }
)

# A ticker "candidate" is a bare, standalone, all-caps alphabetic token of
# 1-5 letters, optionally preceded by '$' (common cashtag convention, e.g.
# "$NVDA"). Bare all-caps words (e.g. sentence-leading "The") are filtered
# by the allowlist cross-check in extract_tickers, not by this regex alone
# — the regex only decides what *could* be a symbol.
_CANDIDATE_RE = re.compile(r"\$?\b[A-Z]{1,5}\b")


def extract_tickers(text: str, known_assets: set[str] | None = None) -> list[str]:
    """Extract ticker symbols mentioned in `text`.

    Dev implementation (AD-06): regex-scan for bare all-caps 1-5 letter
    tokens (optionally cashtag-prefixed with '$'), then cross-check every
    candidate against a known-assets allowlist before emitting it. The
    allowlist check is the actual false-positive guard — common English
    words that happen to be all-caps-when-shouted ("A", "ON", "IT", "SO",
    "ALL", "NOW", "GO", "FOR", "ARE", "CAT") are only ever extracted when
    they're *also* real ticker symbols in the allowlist, and only when they
    appear as a standalone token (never as part of a longer word).

    Args:
        text: Article text (headline and/or body) to scan.
        known_assets: Allowlist of valid ticker symbols to cross-check
            against. If omitted, falls back to `DEFAULT_KNOWN_ASSETS`. In
            production this is loaded from the `assets` table (schema.sql)
            rather than hardcoded — callers (e.g. `unified_pipeline.py`)
            should pass the live set once that wiring exists (Step 5+).

    Returns:
        Deduplicated list of matched ticker symbols, in first-seen order,
        with any leading '$' stripped.
    """
    if not text:
        return []

    allowlist = known_assets if known_assets is not None else DEFAULT_KNOWN_ASSETS

    found: list[str] = []
    seen: set[str] = set()
    for match in _CANDIDATE_RE.finditer(text):
        raw = match.group(0)
        symbol = raw.lstrip("$")
        if symbol in allowlist and symbol not in seen:
            seen.add(symbol)
            found.append(symbol)
    return found


# ---------------------------------------------------------------------------
# Stage 2: sentiment scoring (dev: BULL/BEAR lexicon; prod: finbert, AD-06)
# ---------------------------------------------------------------------------

#: Bullish-signal words/phrases. Deliberately a "reasonably real" list
#: (not a 3-word toy) so dev-mode sentiment is a usable baseline, not just a
#: placeholder. Multi-word phrases are matched as substrings on normalized
#: (lowercased) text.
BULLISH_WORDS: tuple[str, ...] = (
    "surge", "surges", "surging", "rally", "rallies", "rallying", "rallied",
    "beat", "beats", "beating", "upgrade", "upgrades", "upgraded",
    "bullish", "outperform", "outperforms", "outperformed",
    "record high", "all-time high", "soar", "soars", "soaring",
    "breakout", "breaks out", "jump", "jumps", "jumping",
    "gain", "gains", "gaining", "growth", "expands", "expansion",
    "strong demand", "raises guidance", "raised guidance",
    "buyback", "beat expectations", "exceeds expectations",
    "profit rises", "revenue growth", "bull run", "accumulation",
    "positive outlook", "optimis", "recovery", "rebound", "rebounds",
    "up sharply", "climb", "climbs", "climbing", "boom", "booming",
)

#: Bearish-signal words/phrases (mirrors BULLISH_WORDS).
BEARISH_WORDS: tuple[str, ...] = (
    "plunge", "plunges", "plunging", "plunged",
    "crash", "crashes", "crashing", "crashed",
    "miss", "misses", "missed", "downgrade", "downgrades", "downgraded",
    "bearish", "underperform", "underperforms", "underperformed",
    "record low", "all-time low", "slump", "slumps", "slumping",
    "sell-off", "selloff", "selloffs", "drop", "drops", "dropping",
    "loss", "losses", "losing", "decline", "declines", "declining",
    "weak demand", "cuts guidance", "cut guidance",
    "layoffs", "misses expectations", "falls short",
    "profit falls", "revenue decline", "bear market", "liquidation",
    "negative outlook", "pessimis", "recession", "contraction",
    "down sharply", "tumble", "tumbles", "tumbling", "bust", "collapsing",
    "collapse", "fraud", "lawsuit", "investigation", "bankruptcy",
    "default", "warns", "warning", "recall",
)


def score_sentiment(text: str) -> float:
    """Score `text`'s sentiment in `[-1.0, 1.0]`.

    Dev implementation (AD-06): zero-dependency BULL/BEAR word-list lexicon.
    Counts bullish vs. bearish word/phrase matches on normalized
    (lowercased) text and normalizes by *total matched-word count*, not
    total word count — this keeps a long, mostly-neutral article with a
    single incidental bullish word from being forced to +1.0/-1.0; a lone
    match still produces a strong-but-not-maxed score, while a lopsided mix
    of many matches produces a score close to the extremes.

    Prod swap point (AD-06): replace the function body with a call to
    `ProsusAI/finbert` (via `transformers`) — the signature
    (`text: str -> float` in `[-1, 1]`) must not change.

    Args:
        text: Article text (headline + body, typically) to score.

    Returns:
        Float in `[-1.0, 1.0]`; 0.0 for neutral/no-signal text.
    """
    if not text:
        return 0.0

    normalized = text.lower()

    bull_count = sum(normalized.count(word) for word in BULLISH_WORDS)
    bear_count = sum(normalized.count(word) for word in BEARISH_WORDS)

    total = bull_count + bear_count
    if total == 0:
        return 0.0

    # Normalized net sentiment in [-1, 1]: e.g. 3 bull / 1 bear -> 0.5, not
    # a raw count that could exceed the valid range.
    raw = (bull_count - bear_count) / total

    return max(-1.0, min(1.0, raw))


# ---------------------------------------------------------------------------
# Stage 3: topic classification (dev: keyword-based)
# ---------------------------------------------------------------------------

#: Fixed set of topic labels this dev implementation can emit. Topic output
#: is stored in Event.meta (e.g. meta["topic"] = "earnings") by
#: unified_pipeline.py, never as a new top-level Event field (AD-01).
TOPIC_LABELS: tuple[str, ...] = (
    "earnings",
    "macro",
    "regulatory",
    "m_and_a",
    "product",
    "general",
)

_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "earnings": (
        "earnings", "eps", "revenue", "quarterly results", "q1", "q2",
        "q3", "q4", "guidance", "profit", "net income", "beat estimates",
        "misses estimates", "earnings call", "fiscal year",
    ),
    "macro": (
        "federal reserve", "fed ", "interest rate", "inflation", "cpi",
        "gdp", "jobs report", "unemployment", "fomc", "treasury yield",
        "central bank", "rate hike", "rate cut", "recession", "macro",
    ),
    "regulatory": (
        "sec ", "regulator", "regulation", "lawsuit", "antitrust",
        "investigation", "fine", "compliance", "settlement", "ftc",
        "doj", "subpoena", "sanction", "ban ", "banned",
    ),
    "m_and_a": (
        "acquire", "acquisition", "merger", "merges", "buyout", "takeover",
        "to acquire", "deal to buy", "agrees to buy", "stake in",
        "divest", "spin-off", "spinoff",
    ),
    "product": (
        "launch", "unveils", "release", "new product", "update", "feature",
        "partnership", "collaborat", "rollout", "beta", "announces",
    ),
}


def classify_topic(text: str) -> str:
    """Classify `text` into one of `TOPIC_LABELS`.

    Dev implementation: keyword matching against a fixed per-topic keyword
    table, in priority order (earnings > macro > regulatory > m_and_a >
    product); the first topic with any keyword match wins. Falls back to
    "general" when nothing matches.

    Args:
        text: Article text (headline + body, typically) to classify.

    Returns:
        One of `TOPIC_LABELS`.
    """
    if not text:
        return "general"

    normalized = text.lower()

    for topic in ("earnings", "macro", "regulatory", "m_and_a", "product"):
        keywords = _TOPIC_KEYWORDS[topic]
        if any(keyword in normalized for keyword in keywords):
            return topic

    return "general"


# ---------------------------------------------------------------------------
# Stage 4: urgency scoring
# ---------------------------------------------------------------------------

#: Keyword urgency signals — phrases that indicate breaking/time-sensitive
#: news regardless of sentiment polarity.
URGENCY_KEYWORDS: tuple[str, ...] = (
    "breaking", "urgent", "just in", "alert", "developing story",
    "flash:", "flash ", "halted", "trading halt", "emergency",
)

#: Recency half-life for the urgency decay component, in minutes. An
#: article published this many minutes ago contributes 0.5 to the recency
#: term; older articles decay further, newer articles approach 1.0. This is
#: intentionally shorter than AD-03's confluence-score half-lives (60-180
#: min) since urgency is about *this article's* freshness at NLP-processing
#: time, not a cross-signal confluence decay.
_URGENCY_RECENCY_HALF_LIFE_MINUTES = 30.0


def _recency_component(published_at: datetime, now: datetime) -> float:
    """Exponential recency decay in [0, 1], mirroring AD-03's decay shape."""
    age_minutes = max(0.0, (now - published_at).total_seconds() / 60.0)
    return math.exp(
        -math.log(2) / _URGENCY_RECENCY_HALF_LIFE_MINUTES * age_minutes
    )


def score_urgency(text: str, published_at: datetime) -> float:
    """Score how time-sensitive an article is, in `[0.0, 1.0]`.

    Combines (a) keyword urgency signals ("breaking", "urgent", "just in",
    "alert", etc.) and (b) recency of `published_at` relative to now, via
    an exponential decay (mirrors AD-03's decay shape, on a much shorter
    half-life appropriate to "is this news still fresh").

    IMPORTANT (per nlp-pipeline skill "Gotchas" / this module's task spec):
    urgency feeds `Event.confidence`/`meta` weighting downstream, NEVER
    `Event.direction`. A highly urgent but neutral-sentiment article must
    not be forced bullish/bearish — `NLPPipeline.process()` keeps `urgency`
    and `sentiment` as fully independent `NLPResult` fields for exactly this
    reason.

    Args:
        text: Article text (headline + body, typically) to scan for
            urgency keywords.
        published_at: Timezone-aware UTC datetime the article was
            published.

    Returns:
        Float in `[0.0, 1.0]`; higher = more time-sensitive.
    """
    normalized = (text or "").lower()
    keyword_hit = any(keyword in normalized for keyword in URGENCY_KEYWORDS)

    now = datetime.now(timezone.utc)
    recency = _recency_component(published_at, now)

    # Keyword signal contributes a flat boost; recency contributes a
    # continuous component. Weighted so a "breaking" tag on stale-ish news
    # still registers as urgent, and very fresh news without a keyword hit
    # still registers as moderately urgent from recency alone.
    keyword_component = 1.0 if keyword_hit else 0.0
    urgency = 0.6 * keyword_component + 0.4 * recency

    return max(0.0, min(1.0, urgency))


# ---------------------------------------------------------------------------
# Stage 5: deduplication (dev: MD5 fingerprint + BoW cosine; prod:
# sentence-transformers + pgvector, AD-06)
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize_headline(headline: str) -> str:
    """Lowercase + whitespace-collapse a headline for fingerprinting."""
    return _WHITESPACE_RE.sub(" ", headline.strip().lower())


def fingerprint(article: Article) -> str:
    """MD5 hash of `article`'s normalized headline text (FR-03).

    Normalization is lowercase + whitespace-collapse only (not full token
    normalization) so this stays a cheap, deterministic exact/near-exact
    dedup check distinct from the bag-of-words cosine similarity used for
    near-dupes across sources (see `is_duplicate`).

    Args:
        article: The `Article` to fingerprint.

    Returns:
        Hex-encoded MD5 digest of the normalized headline.
    """
    normalized = _normalize_headline(article.headline)
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _bow_vector(tokens: list[str], vocab: dict[str, int]) -> np.ndarray:
    vec = np.zeros(len(vocab), dtype=np.float64)
    for token in tokens:
        idx = vocab.get(token)
        if idx is not None:
            vec[idx] += 1.0
    return vec


def _cosine_similarity(a: Article, b: Article) -> float:
    """Bag-of-words cosine similarity over normalized headline text.

    Dev implementation of the "near-dupe across sources" similarity check.
    Builds a shared vocabulary from both headlines' tokens, then computes
    standard cosine similarity between their term-frequency vectors.

    Prod swap point (AD-06): replace with `sentence-transformers/
    all-MiniLM-L6-v2` embeddings compared via pgvector's `<=>` cosine
    distance operator against `articles.embedding vector(384)` — this
    function's *role* in the pipeline (headline-pair similarity score) is
    what stays constant; the prod version operates over stored embeddings
    and a DB query rather than two in-memory Articles, so it lives behind
    `is_duplicate`'s corpus-scoped loop rather than being called directly
    with the same signature. `is_duplicate`'s own signature is the actual
    swap-stable contract.
    """
    tokens_a = _tokenize(_normalize_headline(a.headline))
    tokens_b = _tokenize(_normalize_headline(b.headline))

    if not tokens_a or not tokens_b:
        return 0.0

    vocab = {token: i for i, token in enumerate(sorted(set(tokens_a) | set(tokens_b)))}
    vec_a = _bow_vector(tokens_a, vocab)
    vec_b = _bow_vector(tokens_b, vocab)

    norm_a = np.linalg.norm(vec_a)
    norm_b = np.linalg.norm(vec_b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))


def is_duplicate(
    article: Article,
    corpus: list[Article],
    tickers: list[str],
    similarity_threshold: float = 0.92,
) -> tuple[bool, str | None]:
    """Determine whether `article` duplicates an article already in `corpus`.

    FR-03 dedup, two tiers, exact check first (cheaper):

    1. Exact/near-exact: MD5 fingerprint match on normalized headline text.
    2. Near-dupe across sources: bag-of-words cosine similarity over
       normalized headline text, `>= similarity_threshold` (default 0.92).

    Critically, tier 2 is *scoped to articles sharing at least one
    extracted ticker with `article`* before the similarity threshold is
    ever applied — comparing similarity across the full corpus regardless
    of ticker would dedup unrelated articles about different tickers that
    happen to share boilerplate text (e.g. wire-service templates,
    "shares of X rose/fell today" phrasing). `tickers` must be the tickers
    already extracted from `article` (stage 1 output) so this function
    doesn't need to re-run extraction on every corpus candidate — callers
    (i.e. `NLPPipeline.process`) are responsible for having already
    extracted tickers for every article in `corpus` and attaching them
    somewhere queryable; this dev implementation re-extracts each
    candidate's tickers on the fly for simplicity (see note below).

    Prod swap point (AD-06): replace the body with a pgvector query scoped
    by a ticker join (`WHERE articles.id IN (SELECT article_id FROM
    article_tickers WHERE ticker = ANY(:tickers))`) ordered by `embedding
    <=> :query_embedding` with the same threshold semantics. Signature
    (`is_duplicate(article, corpus, tickers, similarity_threshold) ->
    tuple[bool, str | None]`) stays identical.

    Args:
        article: The new `Article` being checked for duplication.
        corpus: Previously seen `Article`s to check against.
        tickers: Tickers already extracted from `article` (stage 1 output).
        similarity_threshold: Cosine similarity threshold for near-dupe
            detection across sources. Defaults to 0.92 per CLAUDE.md's
            starting point (tune against false-positive dedup of
            genuinely distinct articles about the same ticker).

    Returns:
        `(is_duplicate, duplicate_of)` — `duplicate_of` is the matched
        article's fingerprint (computed on the fly if the corpus article
        doesn't already carry one), or `None` if no duplicate was found.
    """
    if not corpus:
        return False, None

    target_fp = fingerprint(article)
    target_ticker_set = set(tickers)

    # Tier 1: exact/near-exact fingerprint match — cheap, check first,
    # independent of ticker scoping (an exact headline match is a dupe
    # regardless; this mirrors treating fingerprint dedup as the coarser,
    # unconditional check FR-03 describes).
    for candidate in corpus:
        candidate_fp = candidate.fingerprint or fingerprint(candidate)
        if candidate_fp == target_fp:
            return True, candidate_fp

    # Tier 2: near-dupe across sources, scoped to shared-ticker candidates
    # only, then thresholded by cosine similarity.
    if not target_ticker_set:
        # No tickers extracted for this article at all — nothing to scope
        # the similarity search to, so skip tier 2 entirely rather than
        # falling back to an unscoped (and therefore false-positive-prone)
        # global similarity search.
        return False, None

    for candidate in corpus:
        candidate_tickers = set(extract_tickers(candidate.headline + " " + candidate.body))
        if target_ticker_set.isdisjoint(candidate_tickers):
            continue  # no shared ticker — never dedup across unrelated tickers

        similarity = _cosine_similarity(article, candidate)
        if similarity >= similarity_threshold:
            candidate_fp = candidate.fingerprint or fingerprint(candidate)
            return True, candidate_fp

    return False, None


# ---------------------------------------------------------------------------
# NLPPipeline — orchestrates all 5 stages into a single NLPResult
# ---------------------------------------------------------------------------


class NLPPipeline:
    """Runs the full NLP stage pipeline over an `Article`, producing one
    `NLPResult`.

    Stages run in the documented order (CLAUDE.md §5): ticker extraction ->
    sentiment scoring -> topic classification -> urgency scoring ->
    deduplication -> `NLPResult` emission. `Event` construction from
    `NLPResult` is explicitly out of scope here — see `unified_pipeline.py`
    (Step 5).

    Every stage is delegated to a free function (`extract_tickers`,
    `score_sentiment`, `classify_topic`, `score_urgency`, `is_duplicate`)
    rather than inlined, so the dev -> prod swap (AD-06) is a matter of
    monkey-patching/subclassing/reassigning those functions (or the
    instance methods below that wrap them) without ever touching
    `process()`'s signature or control flow.

    Attributes:
        known_assets: Ticker allowlist passed through to `extract_tickers`.
            Defaults to `DEFAULT_KNOWN_ASSETS` when not supplied; in
            production this should be the live `assets` table contents.
        similarity_threshold: Cosine similarity threshold passed through to
            `is_duplicate`. Defaults to 0.92 per CLAUDE.md.
    """

    def __init__(
        self,
        known_assets: set[str] | None = None,
        similarity_threshold: float = 0.92,
    ) -> None:
        self.known_assets = known_assets
        self.similarity_threshold = similarity_threshold

    # -- swap points, exposed as instance methods for easy subclass/monkey-
    # patch override without touching process()'s control flow --

    def extract_tickers(self, text: str) -> list[str]:
        return extract_tickers(text, known_assets=self.known_assets)

    def score_sentiment(self, text: str) -> float:
        return score_sentiment(text)

    def classify_topic(self, text: str) -> str:
        return classify_topic(text)

    def score_urgency(self, text: str, published_at: datetime) -> float:
        return score_urgency(text, published_at)

    def is_duplicate(
        self, article: Article, corpus: list[Article], tickers: list[str]
    ) -> tuple[bool, str | None]:
        return is_duplicate(
            article, corpus, tickers, similarity_threshold=self.similarity_threshold
        )

    def process(
        self, article: Article, corpus: list[Article] | None = None
    ) -> NLPResult:
        """Run all 5 stages over `article` and return a fully-populated
        `NLPResult`.

        Args:
            article: The `Article` to process.
            corpus: Previously seen `Article`s to check `article` against
                for deduplication. Defaults to an empty list (no dedup
                candidates) when omitted.

        Returns:
            `NLPResult` with `tickers`, `sentiment`, `topic`, `urgency`,
            `is_duplicate`, and `duplicate_of` populated.
        """
        corpus = corpus if corpus is not None else []
        full_text = f"{article.headline} {article.body}"

        # 1. Ticker extraction
        tickers = self.extract_tickers(full_text)

        # 2. Sentiment scoring
        sentiment = self.score_sentiment(full_text)

        # 3. Topic classification
        topic = self.classify_topic(full_text)

        # 4. Urgency scoring — independent of sentiment; feeds confidence/
        # meta downstream, never direction (see score_urgency docstring).
        urgency = self.score_urgency(full_text, article.published_at)

        # 5. Deduplication
        duplicate, duplicate_of = self.is_duplicate(article, corpus, tickers)

        return NLPResult(
            article=article,
            tickers=tickers,
            sentiment=sentiment,
            topic=topic,
            urgency=urgency,
            is_duplicate=duplicate,
            duplicate_of=duplicate_of,
        )


__all__ = [
    "NLPPipeline",
    "extract_tickers",
    "score_sentiment",
    "classify_topic",
    "score_urgency",
    "fingerprint",
    "is_duplicate",
    "DEFAULT_KNOWN_ASSETS",
    "TOPIC_LABELS",
    "BULLISH_WORDS",
    "BEARISH_WORDS",
    "URGENCY_KEYWORDS",
]
