"""AI co-pilot subsystem (per-user DeepSeek / OpenAI-compatible).

Design constraints (see the AI design proposal):
- Read-only: nothing here calls place_order or creates/executes signals.
- Education-only: outputs pass a deterministic 荐股 (stock-tip) guardrail filter.
- Per-user keys: each user supplies their own API key, encrypted at rest.
- Isolated: free-function modules (like services.py), not wired into the god-object.
"""
