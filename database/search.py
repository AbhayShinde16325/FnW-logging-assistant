"""Lightweight, dependency-free text matching for the task search.

The task search used to compare the whole employee sentence with
``ILIKE '%...%'``. "research about MCP" was therefore one literal substring
that never occurred in "Researching about MCP servers".

This module turns both the query and the stored values into small sets of
canonical *concepts*:

* case is ignored and punctuation is a separator,
* stop words ("do", "have", "a", "my", "task", ...) carry no meaning,
* a small English stemmer removes plural and verb endings
  ("servers" -> "server", "researching" -> "research"),
* a conservative synonym map groups related words
  ("investigate", "study", "analysis" -> "research"),
* a fuzzy comparison tolerates spelling mistakes ("reserch").

The database still does the filtering with case-insensitive ``ILIKE`` terms,
but the final decision and the ranking happen here, on the small candidate
set the database returns. No new dependency, no external service and no
embedding is involved.

The functions in this module are pure: they never touch the database.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# --- tokenisation and stop words ---------------------------------------------------

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

# Words that say nothing about *which* task is meant. "task" itself is in the
# list on purpose: "MCP servers task" is about MCP, not about every task.
STOP_WORDS = frozenset(
    """
    a an the this that these those my mine me i we our us you your it its
    do does did is are was were be been being have has had any some all
    of for to in on at with about regarding related around into from by
    and or but not no please can could would should will just like similar
    find search look looking show get list tell give check
    task tasks item items todo
    """.split()
)

# A very small set of high-value concepts. It is deliberately conservative:
# a wrong synonym is worse than a missing one.
_SYNONYM_GROUPS = {
    "research": (
        "research", "researching", "investigate", "investigating",
        "investigation", "study", "studying", "explore", "exploring",
        "exploration", "analysis", "analyse", "analyze", "survey",
    ),
    "complete": (
        "complete", "completed", "completing", "completion",
        "finish", "finished", "done",
    ),
    "develop": (
        "develop", "developing", "development", "build", "building",
        "implement", "implementing", "implementation",
    ),
    "fix": (
        "fix", "fixing", "repair", "repairing", "resolve", "resolving",
        "resolution", "debug", "debugging", "troubleshoot",
    ),
    "bug": ("bug", "bugs", "defect", "defects", "issue", "issues", "error", "errors"),
    "document": ("document", "documents", "documentation", "docs", "doc"),
    "meeting": ("meeting", "meetings", "call", "calls", "sync"),
    "deploy": ("deploy", "deploying", "deployment", "release", "publish"),
    "test": ("test", "tests", "testing", "qa", "verify", "verification"),
    "review": ("review", "reviews", "reviewing"),
    "report": ("report", "reports", "reporting", "summary", "summaries"),
    "server": ("server", "servers", "service", "services"),
    "database": ("database", "databases", "db", "postgres", "postgresql", "sql"),
    "frontend": ("frontend", "ui"),
    "api": ("api", "apis", "endpoint", "endpoints"),
    "mcp": ("mcp",),
}

_STATUS_TERMS = (
    ("in progress", "IN_PROGRESS"),
    ("in_progress", "IN_PROGRESS"),
    ("ongoing", "IN_PROGRESS"),
    ("not started", "PENDING"),
    ("pending", "PENDING"),
    ("todo", "PENDING"),
    ("completed", "COMPLETED"),
    ("complete", "COMPLETED"),
    ("done", "COMPLETED"),
    ("finished", "COMPLETED"),
)

# How much a single matched concept contributes to the relevance score.
_EXACT_WEIGHT = 60
_PARTIAL_WEIGHT = 30
_FUZZY_WEIGHT = 20
_ALL_MATCHED_BONUS = 120
_FULL_PHRASE_BONUS = 150
_PREFIX_PHRASE_BONUS = 300
_EXACT_TITLE_BONUS = 1000
_WORD_ORDER_BONUS = 40

_FUZZY_RATIO = 0.82


def normalize_text(text: str | None) -> str:
    """Lower-case text with punctuation turned into single spaces."""

    return " ".join(_TOKEN_PATTERN.findall((text or "").lower()))


def tokenize(text: str | None) -> list[str]:
    """The lower-case word tokens of a text."""

    return _TOKEN_PATTERN.findall((text or "").lower())


def stem(word: str) -> str:
    """A small, deterministic English stemmer.

    It is not a full Porter stemmer, but it normalises the variations that
    matter for task titles: plural nouns and the common verb endings. Both
    the title and the query go through the same function, so it only has to
    be *consistent*, not linguistically perfect.
    """

    word = (word or "").lower()
    if len(word) <= 3:
        return word

    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("sses"):
        word = word[:-2]
    elif word.endswith(("ches", "shes", "xes", "zes")):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]

    if word.endswith("ing") and len(word) > 5:
        word = word[:-3]
    elif word.endswith("ed") and len(word) > 4:
        word = word[:-2]
    elif word.endswith("ly") and len(word) > 4:
        word = word[:-2]

    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]

    if word.endswith(("at", "bl", "iz")):
        word += "e"

    if word.endswith("e") and len(word) > 3:
        word = word[:-1]

    return word


def _build_synonym_index():
    concept_of: dict[str, str] = {}
    aliases: dict[str, set[str]] = {}

    for name, words in _SYNONYM_GROUPS.items():
        key = stem(name)
        group: set[str] = {key}

        for word in words:
            for token in tokenize(word):
                token_stem = stem(token)
                group.add(token_stem)
                concept_of[token_stem] = key

        aliases[key] = group

    return concept_of, aliases


_CONCEPT_OF, _ALIASES = _build_synonym_index()


def concept_of(word: str) -> str:
    """The canonical concept of one word."""

    word_stem = stem(word)
    return _CONCEPT_OF.get(word_stem, word_stem)


def aliases_for(concept: str) -> set[str]:
    """Every stem that belongs to the concept (including itself)."""

    return _ALIASES.get(concept, {concept})


def query_concepts(text: str | None) -> list[str]:
    """The ordered, de-duplicated concepts of a search query."""

    result: list[str] = []

    for token in tokenize(text):
        if token in STOP_WORDS:
            continue

        value = concept_of(token)

        if not value or value in STOP_WORDS:
            continue

        if len(value) < 2 and not value.isdigit():
            continue

        if value not in result:
            result.append(value)

    return result


def status_from_text(text: str | None) -> str | None:
    """The task status mentioned in a message, if any."""

    normalized = f" {normalize_text(text)} "

    for phrase, status in _STATUS_TERMS:
        if f" {phrase} " in normalized:
            return status

    return None


def without_status(text: str | None) -> str:
    """The text with the words that describe a status removed.

    When the status is already used as a database filter, the ranking must
    not require the title itself to contain "completed".
    """

    cleaned = text or ""

    for phrase, _ in _STATUS_TERMS:
        cleaned = re.sub(
            rf"\b{re.escape(phrase)}\b", " ", cleaned, flags=re.IGNORECASE
        )

    return cleaned


def _prefix(token: str) -> str:
    """A short prefix used to fetch spelling variants from the database."""

    return token[:3] if len(token) >= 4 else token


def sql_terms(text: str | None) -> list[str]:
    """The case-insensitive ``ILIKE`` terms used to fetch candidates.

    The list contains the query words, their stems, their synonyms and a
    short prefix, so a spelling mistake or an inflected word still reaches
    the database. Ranking then discards the weak candidates.
    """

    terms: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip().lower()

        if value and value not in terms:
            terms.append(value)

    for token in tokenize(text):
        if token in STOP_WORDS:
            continue

        add(token)
        token_stem = stem(token)
        add(token_stem)
        add(_prefix(token_stem))

        for alias in aliases_for(concept_of(token)):
            add(alias)
            add(_prefix(alias))

    if not terms:
        add(normalize_text(text))

    return terms


def _fuzzy_match(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 3:
        return False

    return SequenceMatcher(None, left, right).ratio() >= _FUZZY_RATIO


def _concept_weight(concept: str, title_tokens: list[str], title_stems: list[str]) -> int:
    """How strongly one query concept matches the title.

    ``0`` means no match, ``_EXACT_WEIGHT`` an exact/inflected match and
    ``_PARTIAL_WEIGHT``/``_FUZZY_WEIGHT`` the tolerant matches.
    """

    aliases = aliases_for(concept)

    if concept in title_stems:
        return _EXACT_WEIGHT

    if any(alias in title_stems for alias in aliases if alias != concept):
        return _EXACT_WEIGHT

    # A partial word: "resea" or "bus" still finds "researching"/"business".
    if len(concept) >= 3 and any(
        token.startswith(concept) for token in title_tokens
    ):
        return _PARTIAL_WEIGHT

    if len(concept) >= 3 and any(
        title_stem.startswith(concept) for title_stem in title_stems
    ):
        return _PARTIAL_WEIGHT

    # A spelling mistake in the query.
    if any(_fuzzy_match(concept, token) for token in title_tokens):
        return _FUZZY_WEIGHT

    if any(_fuzzy_match(concept, title_stem) for title_stem in title_stems):
        return _FUZZY_WEIGHT

    if any(
        _fuzzy_match(alias, token)
        for alias in aliases
        for token in title_tokens
    ):
        return _FUZZY_WEIGHT

    if any(
        _fuzzy_match(alias, title_stem)
        for alias in aliases
        for title_stem in title_stems
    ):
        return _FUZZY_WEIGHT

    return 0


def match_score(title: str | None, text: str | None):
    """Score one title against the query.

    Returns ``(score, matched_concepts, total_concepts)``. ``score`` is ``0``
    when nothing matches.
    """

    title = title or ""
    concepts = query_concepts(text)

    if not concepts:
        return (0, 0, 0)

    title_tokens = tokenize(title)
    title_stems = [stem(token) for token in title_tokens]
    title_concepts = [concept_of(token) for token in title_tokens]
    title_norm = normalize_text(title)
    query_norm = normalize_text(text)

    score = 0
    matched = 0

    for concept in concepts:
        weight = _concept_weight(concept, title_tokens, title_stems)

        if weight:
            matched += 1
            score += weight

    if title_norm and title_norm == query_norm:
        score += _EXACT_TITLE_BONUS
    elif query_norm and title_norm.startswith(query_norm):
        score += _PREFIX_PHRASE_BONUS
    elif query_norm and query_norm in title_norm:
        score += _FULL_PHRASE_BONUS

    if concepts and matched == len(concepts):
        score += _ALL_MATCHED_BONUS

    if not matched:
        return (0, 0, len(concepts))

    # The concepts in the order of the query are a stronger signal than the
    # same concepts in a different order ("research about MCP" beats
    # "MCP research").
    score += _WORD_ORDER_BONUS * _longest_common_subsequence(
        concepts, title_concepts
    )

    return (score, matched, len(concepts))


def _longest_common_subsequence(left: list[str], right: list[str]) -> int:
    """Length of the longest common subsequence of two concept lists.

    Concepts count as equal when they are the same or a small spelling
    variant of each other, so a typo does not destroy the word order signal.
    """

    if not left or not right:
        return 0

    previous = [0] * (len(right) + 1)

    for item in left:
        current = [0]

        for index, candidate in enumerate(right, start=1):
            if item == candidate or _fuzzy_match(item, candidate):
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[index - 1]))

        previous = current

    return previous[-1]


def relevance(title: str | None, text: str | None) -> float:
    """The fraction of the query concepts present in the title."""

    score, matched, total = match_score(title, text)

    if not total or not score:
        return 0.0

    return matched / total


def rank(rows, text: str | None, fields=("task_title",)):
    """Order candidate rows by relevance, then by id (newest first).

    Rows that match only a fraction of a multi-word query are dropped when
    at least one candidate matches every concept, so a precise lookup stays
    precise while an ambiguous one still returns its best candidates.

    ``fields`` lists the columns that may satisfy the query. The title is
    normally the strongest signal, but a free-text query also covers the
    assignee and the status.
    """

    concepts = query_concepts(text)

    if not concepts:
        return list(rows)

    fields = tuple(fields) or ("task_title",)
    scored = []

    for row in rows:
        best = (0, 0, 0)

        for field in fields:
            candidate = match_score(_value(row, field), text)

            if candidate[0] > best[0]:
                best = candidate

        score, matched, total = best

        if not score:
            continue

        scored.append((score, matched / total if total else 0.0, row))

    if not scored:
        return []

    full = [item for item in scored if item[1] >= 0.999]

    if full:
        scored = full
    else:
        scored = [item for item in scored if item[1] >= 0.5]

    # A shorter title is a more precise hit when two tasks cover the same
    # concepts ("Researching about MCP servers" beats "... servers <tag>").
    scored.sort(
        key=lambda item: (
            item[0],
            -len(tokenize(_value(item[2], "task_title") or "")),
            _value(item[2], "id") or 0,
        ),
        reverse=True,
    )

    return [item[2] for item in scored]


def _value(row, name):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)
