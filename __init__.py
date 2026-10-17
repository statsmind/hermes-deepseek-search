"""deepseek-search — DeepSeek web search + fetch backend for Hermes Agent."""

from __future__ import annotations

from .provider import DeepSeekWebSearchProvider


def register(ctx) -> None:
    """Plugin entry point: make ``web.search_backend: deepseek`` resolvable."""
    ctx.register_web_search_provider(DeepSeekWebSearchProvider())
