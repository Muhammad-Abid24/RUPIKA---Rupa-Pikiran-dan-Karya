"""Safe Telegram error alert formatter (SF-017)."""
from __future__ import annotations

import html
from typing import Optional
from stockforge.errors.codes import NormalizedProviderError
from stockforge.errors.provider_adapters.base import sanitize_sensitive_details


def format_telegram_error_alert(
    error: NormalizedProviderError,
    batch_id: Optional[int] = None,
    item_index: Optional[int] = None,
    total_items: Optional[int] = None,
    fallback_provider: Optional[str] = None,
    fallback_model: Optional[str] = None,
) -> str:
    """Format a safe, clean Telegram alert message without secrets or credentials."""
    icon_map = {
        "critical": "🚨",
        "error": "❌",
        "warning": "⚠️",
        "info": "ℹ️",
    }
    icon = icon_map.get(error.severity, "⚠️")

    lines = [f"{icon} <b>Provider Error Alert: {html.escape(error.code.value)}</b>"]

    # Batch / Item context
    context_parts = []
    if batch_id is not None:
        context_parts.append(f"Batch #{batch_id}")
    if item_index is not None and total_items is not None:
        context_parts.append(f"Image {item_index}/{total_items}")
    elif item_index is not None:
        context_parts.append(f"Item #{item_index}")
    if context_parts:
        lines.append(f"• <b>Scope:</b> {html.escape(' | '.join(context_parts))}")

    # Provider and model
    provider_str = error.provider or "unknown"
    if error.model:
        provider_str += f" ({error.model})"
    lines.append(f"• <b>Source:</b> <code>{html.escape(provider_str)}</code>")

    # Status and raw code
    status_str = f"HTTP {error.http_status}" if error.http_status else "Transport/Client"
    if error.raw_code_redacted:
        status_str += f" [{error.raw_code_redacted}]"
    lines.append(f"• <b>Status:</b> {html.escape(status_str)}")

    # Action / Policy
    action_desc = f"Action: <b>{error.action.upper()}</b>"
    if error.retry_after_seconds is not None:
        action_desc += f" (Retry-After: {error.retry_after_seconds:.1f}s)"
    if error.fallback_allowed:
        if fallback_provider:
            fb = f"{fallback_provider}" + (f" ({fallback_model})" if fallback_model else "")
            action_desc += f" | Routing to: <code>{fb}</code>"
        else:
            action_desc += " | Eligible fallback"
    lines.append(f"• <b>Handling:</b> {action_desc}")

    # Safe user message
    safe_msg = sanitize_sensitive_details(error.safe_user_message) or "An error occurred during provider processing."
    lines.append(f"• <b>Details:</b> {html.escape(safe_msg)}")

    # Request ID
    if error.request_id:
        lines.append(f"• <b>Request ID:</b> <code>{html.escape(error.request_id)}</code>")

    return "\n".join(lines)
