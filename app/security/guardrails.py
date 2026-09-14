"""Dual-boundary sanitisation against prompt injection and data exfiltration.

Threat model
------------
Three untrusted text channels flow into the model's context:

1. **Calendar events** -- titles/descriptions are attacker-writable by anyone who can
   send the user a calendar invite (Gmail auto-creates events from mail, so this is a
   genuine remote-write primitive, not a hypothetical).
2. **Zomato catalogue data** -- restaurant names, dish names and descriptions are
   merchant-controlled free text rendered straight into the planning context.
3. **Direct user input** -- webhook/chat payloads.

Defence layers
--------------
* *Normalise* -- strip zero-width/bidi characters used to smuggle instructions past
  regex filters and human review.
* *Isolate* -- wrap untrusted spans in a nonce-tagged block the model is told never to
  take instructions from. The nonce is per-run and unguessable, so injected text cannot
  forge a closing tag to "break out" of the block.
* *Detect* -- score the span against known injection shapes; above threshold, refuse.
* *Canary* -- a secret token lives only in the system prompt. If it ever appears in model
  output or a proposed tool argument, the system prompt has leaked and the run aborts.

None of this is sufficient on its own, and the module does not pretend otherwise. The
*guarantee* lives in ``app/security/policy.py``, which validates every tool call
deterministically in code. Guardrails lower the probability of a bad plan; the policy
engine bounds the blast radius when one gets through anyway.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass, field

from app.observability.latency import REGISTRY, now_ns

__all__ = [
    "Verdict",
    "sanitize",
    "wrap_untrusted",
    "make_canary",
    "scan_output",
    "new_nonce",
    "InjectionBlocked",
]


class InjectionBlocked(Exception):
    """Raised when untrusted input scores above the block threshold."""

    def __init__(self, reasons: list[str], score: int) -> None:
        super().__init__(f"input blocked (score={score}): {'; '.join(reasons)}")
        self.reasons = reasons
        self.score = score


# Characters with no legitimate use in restaurant names or calendar titles that are
# routinely used to hide payloads. Deleted via str.translate (C-speed, no regex).
_INVISIBLE = {
    0x00AD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2028, 0x2029,
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0x2066, 0x2067,
    0x2068, 0x2069, 0xFEFF,
}
_STRIP_TABLE = dict.fromkeys(_INVISIBLE)

# Each pattern carries a weight. Weights are additive; see `injection_block_threshold`.
_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\b[\s\S]{0,30}?"
            r"\b(?:previous|prior|earlier|above|all|any)\b[\s\S]{0,20}?"
            r"\b(?:instruction|prompt|rule|direction|constraint|guardrail)s?\b",
            re.I,
        ),
        2,
    ),
    (
        "role_hijack",
        re.compile(
            r"(?:^|\n|[.!?]\s)\s*(?:system|assistant|developer|user)\s*[:>\]]|"
            r"\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be|new\s+persona|"
            r"from\s+now\s+on\s+you)\b",
            re.I,
        ),
        2,
    ),
    (
        "delimiter_forgery",
        re.compile(
            r"</?\s*(?:untrusted_data|system|system_prompt|instructions?|tool_result)\b|"
            r"\[/?\s*(?:INST|SYS)\s*\]|<\|\s*(?:im_start|im_end|endoftext)\s*\|>",
            re.I,
        ),
        3,
    ),
    (
        "payment_manipulation",
        re.compile(
            r"\b(?:raise|increase|remove|lift|ignore|disable|bypass)\b[\s\S]{0,40}?"
            r"\b(?:budget|spend(?:ing)?|limit|cap|wallet|mandate|approval)s?\b|"
            r"\b(?:transfer|send|pay|refund|payout)\b[\s\S]{0,30}?"
            r"(?:\b(?:to|using|with|via)\b[\s\S]{0,20}?)?"
            r"\b(?:my\s+)?(?:account|upi|card|wallet|vpa)\b",
            re.I,
        ),
        3,
    ),
    (
        # A UPI virtual payment address has no legitimate reason to appear in a dish
        # description or calendar invite. Treated as a funds-redirection attempt.
        "upi_vpa_redirect",
        re.compile(
            r"\b[\w.\-]{2,}@(?:ybl|okaxis|okhdfcbank|oksbi|okicici|paytm|upi|apl|axl|ibl|"
            r"hdfcbank|icici|sbi|barodampay|freecharge)\b",
            re.I,
        ),
        3,
    ),
    (
        "tool_injection",
        re.compile(
            r"\b(?:call|invoke|execute|run|use)\b[\s\S]{0,20}?"
            r"\b(?:checkout_cart|create_cart|tool|function|api)\b|"
            r"\bcheckout\s+(?:immediately|now|without)\b",
            re.I,
        ),
        3,
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(?:reveal|print|output|repeat|show|dump|leak|echo)\b[\s\S]{0,30}?"
            r"\b(?:system\s+prompt|instructions?|api[\s_-]?key|secret|token|credential)s?\b|"
            r"\b(?:base64|curl|https?://)\S{0,80}\?(?:q|data|payload|d)=",
            re.I,
        ),
        3,
    ),
    (
        "urgency_social_engineering",
        re.compile(
            r"\b(?:urgent|immediately|do\s+not\s+(?:ask|confirm|verify)|"
            r"without\s+(?:asking|confirmation|approval)|"
            r"no\s+need\s+to\s+(?:ask|confirm|check))\b",
            re.I,
        ),
        2,
    ),
)


@dataclass(slots=True)
class Verdict:
    """Outcome of sanitising one untrusted span."""

    text: str
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def clean(self) -> bool:
        return self.score == 0

    def blocked(self, threshold: int) -> bool:
        return self.score >= threshold


def new_nonce() -> str:
    """Unguessable per-run tag so injected text cannot forge a closing delimiter."""
    return secrets.token_hex(8)


def make_canary() -> str:
    return f"CANARY-{secrets.token_hex(12)}"


def _normalize(text: str) -> str:
    # NFKC folds homoglyph/compatibility forms so `ｉｇｎｏｒｅ` matches `ignore`.
    return unicodedata.normalize("NFKC", text.translate(_STRIP_TABLE))


def sanitize(
    text: str,
    *,
    source: str = "unknown",
    max_len: int = 4000,
    threshold: int | None = None,
) -> Verdict:
    """Normalise and score one untrusted span.

    Raises ``InjectionBlocked`` when ``threshold`` is given and the score reaches it.
    """
    start = now_ns()
    try:
        if not text:
            return Verdict(text="")
        truncated = len(text) > max_len
        working = _normalize(text[:max_len] if truncated else text)

        score = 0
        reasons: list[str] = []
        for name, pattern, weight in _PATTERNS:
            if pattern.search(working):
                score += weight
                reasons.append(f"{source}:{name}")

        verdict = Verdict(text=working, score=score, reasons=reasons, truncated=truncated)
        if threshold is not None and verdict.blocked(threshold):
            raise InjectionBlocked(reasons, score)
        return verdict
    finally:
        REGISTRY.record_ns("guardrails.sanitize", now_ns() - start)


def wrap_untrusted(text: str, *, nonce: str, source: str) -> str:
    """Fence an untrusted span inside nonce-tagged delimiters."""
    safe = text.replace(f"untrusted_data_{nonce}", "untrusted_data_REDACTED")
    return (
        f"<untrusted_data_{nonce} source=\"{source}\">\n"
        f"{safe}\n"
        f"</untrusted_data_{nonce}>"
    )


def scan_output(
    text: str, *, canary: str, nonce: str | None = None
) -> list[str]:
    """Egress check on model output / proposed tool arguments.

    Returns a list of violation names; empty means clean.
    """
    start = now_ns()
    try:
        violations: list[str] = []
        if not text:
            return violations
        if canary and canary in text:
            violations.append("canary_leak")
        if nonce and f"untrusted_data_{nonce}" in text:
            # The model is echoing our internal fencing back out -- it has been induced
            # to treat the isolation wrapper as content.
            violations.append("delimiter_echo")
        for name, pattern, _ in _PATTERNS:
            if name in ("exfiltration", "payment_manipulation") and pattern.search(text):
                violations.append(f"output:{name}")
        return violations
    finally:
        REGISTRY.record_ns("guardrails.scan_output", now_ns() - start)
