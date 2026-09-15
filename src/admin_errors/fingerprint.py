"""The fingerprint algorithm: a frozen public contract (ADR 0003).

`Issue.fingerprint` is the grouping key. Changing the normalization order, the regexes, or the
parts fed to `compute()` is a major-version breaking change: existing issues would stop matching
new events and silently split, or unrelated errors would start merging. Do not "improve" this
module without a version bump and a migration story.

Dependency-free by design: only `hashlib`, `re`, `typing`. Frame selection, in-app detection and
settings live in `context.py`/`capture.py` (Phase 3) and are not imported here.
"""

import hashlib
import re
from collections.abc import Sequence

MAX_MESSAGE_LENGTH = 200

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
ISO8601_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
DIGITS_RE = re.compile(r"\d+")
WS_RE = re.compile(r"\s+")


def normalize_message(message: str) -> str:
    """Apply the frozen nine-step normalization. See module docstring for stability rules."""
    text = next((line.strip() for line in message.splitlines() if line.strip()), "")
    text = UUID_RE.sub("<uuid>", text)
    text = ISO8601_RE.sub("<ts>", text)
    text = ADDR_RE.sub("<addr>", text)
    text = HEX_RE.sub("<hex>", text)
    text = QUOTED_RE.sub("<str>", text)
    text = DIGITS_RE.sub("#", text)
    text = WS_RE.sub(" ", text).strip()
    return text[:MAX_MESSAGE_LENGTH]


def qualified_type_name(exc_type: type[BaseException]) -> str:
    return f"{exc_type.__module__}.{exc_type.__qualname__}"


def compute(parts: Sequence[str]) -> str:
    joined = ":".join(parts)
    return hashlib.sha1(joined.encode("utf-8", errors="replace")).hexdigest()


def for_exception(exc_type: type[BaseException], culprit: str, message: str) -> str:
    return compute([qualified_type_name(exc_type), culprit, normalize_message(message)])


def for_message(logger: str, level: str, template: object) -> str:
    text = template if isinstance(template, str) else normalize_message(str(template))
    return compute([logger, level, text])


def for_override(value: object) -> str:
    return compute([str(value)])
