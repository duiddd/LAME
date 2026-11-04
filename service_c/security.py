"""Multi-tier Prompt Injection & Safety Guardrails for Service C (Decision D6).

Implements:
1. Pre-Retrieval Heuristic Screen on user queries (blocks jailbreaks, system overrides,
   and out-of-scope trading/investment advice commands).
2. News Content Sanitizer on ingested news items (neutralizes embedded prompt-injection
   directives inside untrusted news articles so they cannot hijack RAG synthesis).
3. XML Context Isolation (<retrieved_evidence id="...">) to separate untrusted data
   from system instructions.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SecurityScreenResult:
    """Outcome of screening user queries or news text."""

    is_safe: bool
    reason_code: Optional[str] = None
    explanation: Optional[str] = None
    matched_pattern: Optional[str] = None


# Layer 1: Adversarial prompt-injection patterns
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass)\s+(all\s+|any\s+|your\s+)?(previous|prior|above|system|safety|initial)\s+(instructions?|prompts?|rules?|directives?|constraints?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_role_hijack",
        re.compile(
            r"(\bsystem\s*override\b|\byou\s+are\s+now\s+(in\s+)?(developer|dan|unrestricted|god|admin)\s+mode\b|\bact\s+as\s+an?\s+unrestricted\b|\bnew\s+system\s+prompt\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_leak_attempt",
        re.compile(
            r"\b(reveal|print|output|repeat|show|dump)\s+(your\s+|the\s+)?(exact\s+|full\s+)?(system\s+prompt|hidden\s+instructions|secret\s+instructions|developer\s+message)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "delimiter_breakout",
        re.compile(
            r"(</?\s*retrieved_evidence\b|</?\s*system_prompt\b|<\s*script\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "fake_citation_injection",
        re.compile(
            r"\b(fabricate|invent|make\s+up)\s+(a\s+)?(citation|news_id|source)\b",
            re.IGNORECASE,
        ),
    ),
)

# Out-of-scope investment advice / order execution requests (PROPOSAL.md: "gives no investment advice and makes no trading decisions")
INVESTMENT_ADVICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(should\s+i\s+(buy|sell|long|short|invest|hold)|give\s+me\s+(financial|investment|trading)\s+advice|execute\s+an?\s+order|place\s+a\s+(buy|sell)\s+order\s+for\s+me|what\s+price\s+will\s+[a-z0-9\-]+\s+hit\s+tomorrow)\b",
        re.IGNORECASE,
    ),
)


def screen_user_query(query: str) -> SecurityScreenResult:
    """Screens an incoming user question before FTS5 retrieval or LLM invocation."""
    cleaned = query.strip()
    if not cleaned:
        return SecurityScreenResult(
            is_safe=False,
            reason_code="unsafe_injection",
            explanation="Empty query rejected.",
        )

    for label, pattern in INJECTION_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return SecurityScreenResult(
                is_safe=False,
                reason_code="unsafe_injection",
                explanation=(
                    f"Query blocked by Prompt-Injection Guardrail ({label}). "
                    "Service C only answers factual questions about market activity and news."
                ),
                matched_pattern=match.group(0),
            )

    for pattern in INVESTMENT_ADVICE_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return SecurityScreenResult(
                is_safe=False,
                reason_code="investment_advice",
                explanation=(
                    "Service C is a market explanation analyst off the trading path. "
                    "It does not provide investment advice, price predictions, or execute orders."
                ),
                matched_pattern=match.group(0),
            )

    return SecurityScreenResult(is_safe=True)


def sanitize_news_text(text: str) -> tuple[str, bool]:
    """Neutralizes embedded prompt-injection directives inside untrusted news articles.

    Returns (sanitized_text, was_modified).
    Any injected instruction inside a news item is replaced with a visible
    `[REDACTED_INJECTION_ATTEMPT]` marker and XML tags are escaped so the article
    cannot break out of `<retrieved_evidence>` tags or alter LLM behavior.
    """
    modified = False
    out = text

    # Escape any attempt to close or forge XML evidence tags
    if "<retrieved_evidence" in out.lower() or "</retrieved_evidence" in out.lower():
        out = re.sub(r"</?\s*retrieved_evidence[^>]*>", "[REDACTED_XML_TAG]", out, flags=re.IGNORECASE)
        modified = True

    # Redact sentences/clauses containing prompt injection directives
    sentence_override = re.compile(
        r"(\[?\s*(SYSTEM\s*OVERRIDE|IMPORTANT\s*INSTRUCTION|IGNORE\s*PREVIOUS\s*INSTRUCTIONS|ADMIN\s*NOTE)[^\.\n]*[\.\n]?)",
        re.IGNORECASE,
    )
    if sentence_override.search(out):
        out = sentence_override.sub("[REDACTED_PROMPT_INJECTION] ", out)
        modified = True

    for _, pattern in INJECTION_PATTERNS:
        if pattern.search(out):
            out = pattern.sub("[REDACTED_PROMPT_INJECTION]", out)
            modified = True

    return out.strip(), modified


def format_isolated_evidence_block(retrieved_items: list[dict]) -> str:
    """Wraps retrieved FTS5 news articles inside strict XML context fences (Decision D6)."""
    blocks: list[str] = []
    for item in retrieved_items:
        safe_headline, _ = sanitize_news_text(item["headline"])
        safe_body, _ = sanitize_news_text(item["body"])
        symbols_str = ", ".join(item.get("symbols", []))
        blocks.append(
            f'<retrieved_evidence id="{item["news_id"]}" '
            f'timestamp_ms="{item["timestamp_ms"]}" '
            f'symbols="{symbols_str}" '
            f'bm25_score="{item.get("bm25_score", 0.0):.4f}">\n'
            f"Headline: {safe_headline}\n"
            f"Body: {safe_body}\n"
            f"</retrieved_evidence>"
        )
    return "\n\n".join(blocks)
