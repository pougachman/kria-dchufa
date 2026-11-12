"""Thin wrapper around the Claude Message Batches API.

Screening code talks to this class only, and gets plain dictionaries back, so tests can replace
it with a fake and never call the network. Batches cost half the standard price and finish
within 24 hours (usually within an hour).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import anthropic

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class ClaudeBatches:
    def __init__(self, api_key: str | None = None, client: anthropic.Anthropic | None = None):
        if client is None:
            if not api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Add it as an environment variable "
                    "(on Railway: the service's Variables tab)."
                )
            client = anthropic.Anthropic(api_key=api_key)
        self.client = client

    def create(self, requests: list[dict]) -> str:
        """Submit requests ({"custom_id", "params"}) as one batch and return its id."""
        batch = self.client.messages.batches.create(requests=requests)
        return batch.id

    def status(self, batch_id: str) -> dict:
        b = self.client.messages.batches.retrieve(batch_id)
        return {
            "status": b.processing_status,  # in_progress | canceling | ended
            "created_at": b.created_at,
            "ended_at": b.ended_at,
            "counts": b.request_counts.model_dump(),
        }

    def results(self, batch_id: str) -> Iterator[dict]:
        """One dict per request, in any order: always key by custom_id."""
        for item in self.client.messages.batches.results(batch_id):
            yield result_to_dict(item)


def result_to_dict(item) -> dict:
    """Convert one SDK batch result into a plain dict."""
    res = item.result
    out = {
        "custom_id": item.custom_id,
        "type": res.type,  # succeeded | errored | canceled | expired
        "model": None,
        "stop_reason": None,
        "text": None,
        "usage": dict.fromkeys(USAGE_FIELDS, 0),
        "error": None,
    }
    if res.type == "succeeded":
        msg = res.message
        out["model"] = msg.model
        out["stop_reason"] = msg.stop_reason
        out["text"] = "".join(b.text for b in msg.content if b.type == "text") or None
        for f in USAGE_FIELDS:
            out["usage"][f] = getattr(msg.usage, f, None) or 0
    elif res.type == "errored":
        err = getattr(res.error, "error", None)
        out["error"] = f"{getattr(err, 'type', 'error')}: {getattr(err, 'message', '')}".strip()
    return out


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
