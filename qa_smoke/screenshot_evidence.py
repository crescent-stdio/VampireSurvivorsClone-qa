"""Finalize blind final-frame captures after campaign inspection and scoring."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .memory import sanitize_error_type

PENDING_SCREENSHOT_NAME = "final-frame.pending.png"
_OPAQUE_TRACE_ID = re.compile(r"[0-9a-f]{16,64}")


@dataclass(frozen=True)
class EvidenceScreenshot:
    path: str | None = None
    error: str = ""
    retention_axes: tuple[str, ...] = ()


def finalize_evidence_screenshot(
    *,
    campaign_root: Path,
    trace_output_dir: Path,
    opaque_trace_id: str,
    screenshot_path: str | None,
    screenshot_error: str,
    retention_axes: Sequence[str],
) -> EvidenceScreenshot:
    """Move retained evidence to an opaque public path or remove the pending frame."""

    axes = tuple(dict.fromkeys(str(axis) for axis in retention_axes if str(axis)))
    error = sanitize_error_type(screenshot_error) if screenshot_error else ""
    if not _OPAQUE_TRACE_ID.fullmatch(opaque_trace_id):
        return EvidenceScreenshot(error="ValueError", retention_axes=axes)

    final_relative = f"screenshots/{opaque_trace_id}.png"
    final_path = campaign_root / final_relative
    pending_path = trace_output_dir / PENDING_SCREENSHOT_NAME
    if screenshot_path == final_relative:
        if not axes:
            try:
                final_path.unlink(missing_ok=True)
            except OSError as exception:
                error = error or sanitize_error_type(type(exception).__name__) or "OSError"
            return EvidenceScreenshot(error=error)
        if final_path.is_file():
            return EvidenceScreenshot(final_relative, error, axes)
        return EvidenceScreenshot(
            error=error or "FileNotFoundError",
            retention_axes=axes,
        )

    if not screenshot_path:
        for artifact_path in (pending_path, final_path):
            try:
                artifact_path.unlink(missing_ok=True)
            except OSError as exception:
                error = (
                    error
                    or sanitize_error_type(type(exception).__name__)
                    or "OSError"
                )
        return EvidenceScreenshot(error=error, retention_axes=axes)
    if screenshot_path != PENDING_SCREENSHOT_NAME:
        return EvidenceScreenshot(error=error or "ValueError", retention_axes=axes)

    if not pending_path.is_file():
        try:
            final_path.unlink(missing_ok=True)
        except OSError as exception:
            error = error or sanitize_error_type(type(exception).__name__) or "OSError"
        return EvidenceScreenshot(
            error=error or "FileNotFoundError",
            retention_axes=axes,
        )
    try:
        if not axes:
            pending_path.unlink()
            final_path.unlink(missing_ok=True)
            return EvidenceScreenshot(error=error)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pending_path, final_path)
        return EvidenceScreenshot(final_relative, error, axes)
    except OSError as exception:
        return EvidenceScreenshot(
            error=error or sanitize_error_type(type(exception).__name__) or "OSError",
            retention_axes=axes,
        )
