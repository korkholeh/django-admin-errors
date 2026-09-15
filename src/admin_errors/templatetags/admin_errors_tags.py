"""Pure template tags/filters for the admin UI (spec section 12).

Every function here takes integers and already-scrubbed payload dicts and returns either a plain
string (auto-escaped by the template that renders it) or, for the two SVG-producing tags, HTML
built with `format_html`/`mark_safe` over escaped parts only — never over a payload string. No
tag here queries the database; the querying happens in `admin.py`.
"""

from __future__ import annotations

import datetime
from typing import Any

from django import template
from django.db.models import Model
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.utils.timezone import now as tz_now
from django.utils.translation import gettext_lazy as _

from admin_errors.models import Issue

register = template.Library()

_SPARKLINE_WIDTH = 120
_SPARKLINE_HEIGHT = 24
_BAR_CHART_WIDTH = 600
_BAR_CHART_HEIGHT = 120

_STATUS_LABELS = {
    Issue.Status.OPEN: _("Open"),
    Issue.Status.RESOLVED: _("Resolved"),
    Issue.Status.IGNORED: _("Ignored"),
}


def _utc_today() -> datetime.date:
    return tz_now().astimezone(datetime.timezone.utc).date()


def _zero_filled_series(
    counts: Any, days: int, *, end: datetime.date | None = None
) -> tuple[list[datetime.date], list[int]]:
    """`days` UTC dates ending at `end` (default today), each zero-filled from `counts`.

    `counts` is anything iterable of objects with `.date`/`.count` (an `IssueDailyCount` queryset
    or list, e.g. `Issue.recent_counts` from the changelist `Prefetch`).
    """
    end = end or _utc_today()
    by_date = {item.date: item.count for item in counts}
    dates = [end - datetime.timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    values = [by_date.get(date, 0) for date in dates]
    return dates, values


@register.simple_tag
def ae_sparkline(issue: Issue, days: int = 14) -> SafeString:
    """120x24 inline SVG polyline, spec section 12.2 "Trend" column."""
    counts = getattr(issue, "recent_counts", None)
    if counts is None:
        counts = issue.daily_counts.all()
    dates, values = _zero_filled_series(counts, days)
    return _render_sparkline(dates, values)


def _series_label(dates: list[datetime.date], values: list[int]) -> str:
    pairs = ", ".join(
        f"{date.isoformat()}: {value}" for date, value in zip(dates, values, strict=True)
    )
    return str(
        _("Occurrences per day (UTC), last %(n)d days: %(values)s")
        % {"n": len(values), "values": pairs}
    )


def _render_sparkline(
    dates: list[datetime.date],
    values: list[int],
    width: int = _SPARKLINE_WIDTH,
    height: int = _SPARKLINE_HEIGHT,
) -> SafeString:
    n = len(values)
    label = _series_label(dates, values)
    if n == 0:
        return format_html(
            '<svg width="{}" height="{}" class="ae-sparkline" role="img" aria-label="{}"></svg>',
            width,
            height,
            label,
        )
    max_value = max(values) or 1
    step = width / max(n - 1, 1)
    pad = 2
    points = " ".join(
        f"{i * step:.1f},{height - pad - (value / max_value) * (height - 2 * pad):.1f}"
        for i, value in enumerate(values)
    )
    return format_html(
        '<svg width="{}" height="{}" viewBox="0 0 {} {}" class="ae-sparkline" role="img" '
        'aria-label="{}"><polyline points="{}" fill="none" stroke="currentColor" '
        'stroke-width="1.5" /></svg>',
        width,
        height,
        width,
        height,
        label,
        points,
    )


@register.simple_tag
def ae_bar_chart(counts: Any, days: int = 30) -> SafeString:
    """30-day inline SVG bar chart for the detail page's "Occurrences" section."""
    dates, values = _zero_filled_series(counts, days)
    return _render_bar_chart(dates, values)


def _render_bar_chart(
    dates: list[datetime.date],
    values: list[int],
    width: int = _BAR_CHART_WIDTH,
    height: int = _BAR_CHART_HEIGHT,
) -> SafeString:
    n = len(values)
    label = _series_label(dates, values)
    if n == 0:
        return format_html(
            '<svg width="{}" height="{}" class="ae-bar-chart" role="img" aria-label="{}"></svg>',
            width,
            height,
            label,
        )
    max_value = max(values) or 1
    slot = width / n
    bar_width = max(slot * 0.7, 1)
    bars = format_html_join(
        "",
        '<rect x="{}" y="{}" width="{}" height="{}" class="ae-bar"><title>{}</title></rect>',
        (
            (
                f"{i * slot + (slot - bar_width) / 2:.1f}",
                f"{height - (value / max_value) * (height - 4):.1f}",
                f"{bar_width:.1f}",
                f"{(value / max_value) * (height - 4):.1f}",
                f"{date.isoformat()}: {value}",
            )
            for i, (date, value) in enumerate(zip(dates, values, strict=True))
        ),
    )
    return format_html(
        '<svg width="{}" height="{}" viewBox="0 0 {} {}" class="ae-bar-chart" role="img" '
        'aria-label="{}">{}</svg>',
        width,
        height,
        width,
        height,
        label,
        bars,
    )


@register.simple_tag
def ae_status_badge(issue: Issue) -> SafeString:
    """`open` + `resolved_at` set renders as **regressed** (spec section 6.1)."""
    if issue.status == Issue.Status.OPEN and issue.resolved_at is not None:
        return format_html('<span class="ae-badge ae-badge--regressed">{}</span>', _("Regressed"))
    modifier = issue.status
    label = _STATUS_LABELS.get(issue.status, issue.status)
    return format_html('<span class="ae-badge ae-badge--{}">{}</span>', modifier, label)


@register.simple_tag
def ae_level_badge(level: str) -> SafeString:
    label = dict(Issue.Level.choices).get(level, level)
    return format_html('<span class="ae-badge ae-badge--level-{}">{}</span>', level, label)


@register.simple_tag(name="ae_traceback_blocks")
def ae_traceback_blocks(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Root-cause-first traceback blocks with the CPython-style separator text between them.

    `payload["exception"]["chain"]` is outermost first (`context.build_exception_block`); this
    reverses it, and picks the separator for the pair `(blocks[i-1], blocks[i])` from the *outer*
    entry's (`blocks[i]`'s, in original chain order) `cause` flag.
    """
    exception = (payload or {}).get("exception")
    if not exception:
        return []
    chain = list(reversed(exception.get("chain") or []))
    blocks = []
    for i, entry in enumerate(chain):
        separator = None
        if i > 0:
            separator = (
                _("The above exception was the direct cause of the following exception:")
                if entry.get("cause")
                else _("During handling of the above exception, another exception occurred:")
            )
        blocks.append({"exc": entry, "separator": separator})
    return blocks


@register.filter
def ae_compact_number(value: Any) -> str:
    """`1234` -> `"1.2k"`, `1200000` -> `"1.2m"`. Falls back to `str(value)` on bad input."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    for threshold, suffix in ((1_000_000, "m"), (1_000, "k")):
        if abs(number) >= threshold:
            scaled = number / threshold
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return str(number)


@register.filter
def ae_frame_label(frame: dict[str, Any]) -> str:
    """`module.function (file:lineno)` for a traceback frame."""
    module = frame.get("module") or "?"
    function = frame.get("function") or "?"
    filename = frame.get("filename") or "?"
    lineno = frame.get("lineno")
    return f"{module}.{function} ({filename}:{lineno})"


@register.filter
def ae_value(value: Any) -> str:
    """Safe scalar rendering for `dict_table.html`: `None` -> em dash, collections flattened."""
    if value is None or value == "":
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}={item}" for key, item in value.items())
    if isinstance(value, Model):
        return str(value)
    return str(value)


__all__ = [
    "ae_bar_chart",
    "ae_compact_number",
    "ae_frame_label",
    "ae_level_badge",
    "ae_sparkline",
    "ae_status_badge",
    "ae_traceback_blocks",
    "ae_value",
    "register",
]
