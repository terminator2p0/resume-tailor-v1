"""Typed boundary between deterministic agents and local model inference."""

from __future__ import annotations

from typing import Any, Protocol


class LocalModel(Protocol):
    def generate(
        self, agent: str, system: str, payload: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a locally validated structured model response."""
        ...
