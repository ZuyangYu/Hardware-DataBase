"""Hardware-aware query normalization for FTS5 (design §8.3).

Hardware documents are full of tokens the default ``unicode61`` tokenizer and
FTS5 query syntax both handle badly: ``+12V``, ``VDD_3V3``, ``R1/R2``,
``STM32H7*`` and continuous CJK text. User input is therefore **never**
concatenated into a MATCH expression. This module separates:

- the raw query (kept for logging/llm context), and
- FTS-safe MATCH expressions for the sparse retriever, plus
- exact identifier candidates for the dedicated exact retriever.
"""

from __future__ import annotations

import re

# Identifier-ish tokens: alphanumeric runs with internal _ . / + - # and digits,
# e.g. STM32H743ZI, TPS62130, VDD_3V3, ETH_TXP, +12V, R1, U2, CAN_H.
_IDENTIFIER_RE = re.compile(
    r"(?<![\w])"
    r"[+\-#]?[A-Za-z][A-Za-z0-9_.+\-/#]{1,40}"
    r"|"
    r"[+\-]?\d+(?:\.\d+)?(?:V|A|Hz|kHz|MHz|mA|mV|uF|nF|pF|k\b|K\b)"
    r"(?![\w])"
)
# Pure reference designators: R1, C2, U3, D4, Q5, J6, TP7, FB8, L9, SW10...
_REFDES_RE = re.compile(r"^(R|C|U|D|Q|J|TP|FB|L|SW|Y|K|X|RN|LED|TVS)\d{1,4}$", re.IGNORECASE)
# Net/power names: VDD_3V3, CAN_H, ETH_TXP, +12V, -5V...
_NETNAME_RE = re.compile(r"^[+\-]?[A-Za-z][A-Za-z0-9_]{1,39}$")
_PARTNO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-\.]{3,40}$")

_FTS_UNSAFE = re.compile(r'[(){}\[\]"\'^*&,:!?]')
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def extract_identifiers(query: str) -> list[str]:
    """Return hardware identifier candidates worth an exact lookup.

    Ordered by specificity: part numbers/net names before bare refdes so a
    fused ranker can weight them accordingly. Duplicates collapse, case is
    preserved (exact retriever normalizes internally).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for token in _IDENTIFIER_RE.findall(query or ""):
        token = token.strip()
        if not token or token.lower() in seen:
            continue
        compact = token.replace(" ", "")
        if _REFDES_RE.match(compact) or _NETNAME_RE.match(compact) or _PARTNO_RE.match(compact):
            if not compact.isalpha() or len(compact) <= 2:
                seen.add(token.lower())
                ordered.append(compact)
    return ordered[:16]


def fts_safe_term(term: str) -> str:
    """Quote one term for a unicode61 FTS5 MATCH expression.

    Double quotes are doubled inside a quoted phrase, which is the only
    escaping FTS5 understands — this keeps ``+12V``, ``A/B`` and CJK
    sequences queryable without syntax errors.
    """
    cleaned = _FTS_UNSAFE.sub(" ", str(term or "")).strip()
    if not cleaned:
        return ""
    return '"{}"'.format(cleaned.replace('"', '""'))


def build_fts_match(query: str, *, max_terms: int = 12) -> str:
    """Build a safe OR-combined MATCH expression from the raw query.

    - CJK runs (unicode61 has no CJK word boundaries) are emitted as quoted
      2-gram phrase groups so continuous Chinese text stays searchable.
    - Latin tokens become quoted terms; identifier-like tokens keep their
      punctuation by being quoted rather than tokenized.
    """
    query = str(query or "").strip()
    if not query:
        return ""
    terms: list[str] = []

    for identifier in extract_identifiers(query):
        if identifier and fts_safe_term(identifier):
            terms.append(fts_safe_term(identifier))

    for cjk_run in _CJK_RE.findall(query):
        pass  # handled below with full-run extraction

    # Extract contiguous CJK runs (not single chars) for n-gram phrases.
    for run in re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]{1,16}", query):
        grams = _cjk_ngrams(run)
        for gram in grams:
            quoted = fts_safe_term(gram)
            if quoted and quoted not in terms:
                terms.append(quoted)

    # Remaining latin words (skip ones already covered by identifiers).
    covered = {t.lower().strip('"') for t in terms}
    for word in re.findall(r"[A-Za-z0-9_.+\-/#]{2,}", query):
        safe = fts_safe_term(word)
        if not safe or safe.lower().strip('"') in covered:
            continue
        terms.append(safe)
        covered.add(safe.lower().strip('"'))
        if len(terms) >= max_terms:
            break

    return " OR ".join(terms[:max_terms])


def _cjk_ngrams(run: str, n: int = 2) -> list[str]:
    run = run.strip()
    if len(run) <= n:
        return [run] if run else []
    return [run[i:i + n] for i in range(len(run) - n + 1)]


def normalize_identifier(value: str) -> str:
    """Canonical form used for exact-identifier matching.

    Lowercase, strip separator noise: ``VDD_3V3`` == ``vdd-3v3`` == ``VDD.3V3``.
    """
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def identifier_variants(value: str) -> tuple[str, str, str]:
    """(raw, normalized, prefix-normalized) forms for the exact retriever."""
    raw = str(value or "").strip()
    normalized = normalize_identifier(raw)
    return raw, normalized, normalized[: max(1, len(normalized) - 1)]
