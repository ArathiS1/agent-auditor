"""Classify the text of an error message into a coarse kind.

This is keyword matching on whatever message the agent recorded. It works well when libraries put
their standard wording in the message (timeouts, HTTP status codes, rate limits) and returns
"other" when the message says nothing useful ("Exception: failed"). It never guesses beyond the text.

Measured on 270 realistic messages written independently of this code (30 per kind, 9 kinds): the
first version scored 74%. After one round of fixes on that set, a second independent set of 270
messages, never used for tuning, scored 83% (82% on the 240 messages that carry a signal). Misses are
mostly long-tail vendor phrasing ("SoftTimeLimitExceeded", "BrokerNotAvailable"); add your own with
`add_error_pattern`.
"""
from __future__ import annotations

import re

# Order matters: the first matching kind wins. Context-length and rate-limit messages often also
# contain generic words, and a "504 Gateway Timeout" is better read as a timeout than a server error.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("context_length", re.compile(
        r"context[ _]length|context[ _]window|maximum context|too many tokens|token limit|context_length_exceeded|"
        r"(?:prompt|input|request|message|text|context|sequence)s? (?:is |are |was )?too (?:long|large)|too (?:long|large) for|"
        r"string too long|maximum (?:prompt|input|sequence|allowed)|max(?:imum)? sequence length|\bn_ctx\b|"
        r"exceeds? (?:the )?(?:model )?(?:limit|maximum)|input length .{0,40}exceeds")),
    ("rate_limit", re.compile(r"rate[ _-]?limit|ratelimit|\b429\b|too many (?:concurrent )?requests|quota|throttl")),
    ("timeout", re.compile(
        r"time[ -]?out|timed[ -]?out|deadline|\betimedout\b|took too long|time limit|wall-clock|"
        r"did not complete in time|cancell?ed after \d+|stalled for")),
    ("auth", re.compile(
        r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|invalid[ _]api[ _]key|incorrect api key|authenticat|authori[sz]|"
        r"permission denied|api key|access denied|expired|invalid_grant|invalid_auth|not_authed|credentials|"
        r"lacks permission|invalid[_ ]token")),
    ("connection", re.compile(
        r"connection (?:refused|reset|error|aborted|closed|failed|broken|pool|lost)|econnre(?:set|fused)|max retries exceeded|"
        r"name or service not known|temporary failure in name resolution|network is unreachable|unreachable|broken pipe|"
        r"server closed the connection|failed to establish|ssl(?:error)?\b|disconnect|no route to host|"
        r"(?:failed|unable|could ?n[o']t|cannot|can't) (?:to )?connect|couldn'?t connect|handshake|"
        r"remoteprotocolerror|incompleteread|autoreconnect|no close frame|proxyerror|tunnel connection failed|reset reason")),
    ("server_error", re.compile(
        r"\b50[0-4]\b|\b529\b|5xx|internal (?:server )?error|bad gateway|service (?:is )?(?:temporarily )?unavailable|unavailable|"
        r"server error|overloaded|upstream|not primary|notwritableprimary|server is busy")),
    ("not_found", re.compile(
        r"\b404\b|not found|could not be found|could not find|does not exist|no such|no (?:row|document|result|record)s? "
        r"(?:was |were )?(?:found|in result)|noresultfound|no documents? found")),
    ("code_or_input", re.compile(
        r"valueerror|keyerror|typeerror|attributeerror|indexerror|nameerror|assertionerror|zerodivision|syntax ?error|"
        r"jsondecode|json\.decoder|expecting value|not valid json|invalid json|unterminated string|"
        r"validationerror|validation error|invalid literal|malformed|could not parse|parsing failed|parse error|parsererror|outputparser|"
        r"recursionerror|unexpected keyword|missing \d+ required|invalid |integrityerror|violates|not-?null|bad request|\b400\b|"
        r"invalid_request|must be between|expected .{0,40} but got|nonetype")),
)

KINDS = tuple(k for k, _ in _RULES) + ("other",)
_CUSTOM: list[tuple[str, re.Pattern[str]]] = []


def add_error_pattern(kind: str, pattern: str) -> None:
    """Teach the matcher a phrase specific to your own tools and services, for example
    add_error_pattern("timeout", r"softtimelimitexceeded|max_execution_time"). Custom patterns are
    checked before the built-in ones. Patterns are matched against the lower-cased message."""
    if kind not in KINDS or kind == "other":
        raise ValueError(f"kind must be one of {KINDS[:-1]}")
    _CUSTOM.append((kind, re.compile(pattern)))


def clear_custom_patterns() -> None:
    _CUSTOM.clear()


def error_kind(text: str | None) -> str:
    """Return one of KINDS for an error message ('other' if nothing recognisable is in it)."""
    if not text:
        return "other"
    low = str(text).lower()
    for kind, pattern in (*_CUSTOM, *_RULES):
        if pattern.search(low):
            return kind
    return "other"


def is_informative(text: str | None) -> bool:
    """Whether an error message carries a recognisable signal (anything other than 'other')."""
    return error_kind(text) != "other"
