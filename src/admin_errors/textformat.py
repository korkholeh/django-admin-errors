"""Plain-text traceback rendering (spec section 12.3), shared by the notification email body and
the admin's "Copy as text" button.

Pure function, no Django imports beyond `gettext`: `notifications.py` (the mail layer) and
`admin.py` (the admin layer) both need it, and neither should import the other's layer.
"""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext

__all__ = ["format_traceback_text"]


def _traceback_blocks(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Root-cause-first blocks with a separator, same ordering as `ae_traceback_blocks`."""
    exception = (payload or {}).get("exception")
    if not exception:
        return []
    chain = list(reversed(exception.get("chain") or []))
    blocks = []
    for i, entry in enumerate(chain):
        separator = None
        if i > 0:
            separator = (
                gettext("The above exception was the direct cause of the following exception:")
                if entry.get("cause")
                else gettext("During handling of the above exception, another exception occurred:")
            )
        blocks.append({"exc": entry, "separator": separator})
    return blocks


def _render_frame(frame: dict[str, Any], *, include_locals: bool) -> list[str]:
    filename = frame.get("filename") or "?"
    lineno = frame.get("lineno")
    function = frame.get("function") or "?"
    lines = [f'  File "{filename}", line {lineno}, in {function}']
    context_line = (frame.get("context_line") or "").strip()
    if context_line:
        lines.append(f"    {context_line}")
    if include_locals:
        for name, value in (frame.get("vars") or {}).items():
            lines.append(f"    {name} = {value}")
    return lines


def _render_block(block: dict[str, Any], *, include_locals: bool, max_frames: int | None) -> str:
    exc = block["exc"]
    lines = [gettext("Traceback (most recent call last):")]
    frames = exc.get("frames") or []
    if max_frames:
        frames = frames[-max_frames:]
    for frame in frames:
        lines.extend(_render_frame(frame, include_locals=include_locals))
    module = exc.get("module") or ""
    exc_type = exc.get("type") or ""
    qualified_type = f"{module}.{exc_type}" if module else exc_type
    value = exc.get("value") or ""
    lines.append(f"{qualified_type}: {value}" if value else qualified_type)
    text = "\n".join(lines)
    if block["separator"]:
        text = f"{block['separator']}\n{text}"
    return text


def format_traceback_text(
    payload: dict[str, Any] | None,
    *,
    include_locals: bool = False,
    max_frames: int | None = None,
) -> str:
    """CPython-style plain traceback for a spec section 6.4 payload, root-cause-first.

    Tolerates a missing/malformed payload: a message-only payload (or `None`) renders just the
    message (or an empty string) instead of raising.
    """
    blocks = _traceback_blocks(payload)
    if not blocks:
        message = (payload or {}).get("message")
        return str(message) if message else ""
    return "\n\n".join(
        _render_block(block, include_locals=include_locals, max_frames=max_frames)
        for block in blocks
    )
