"""Adaptive LLM Router (ALR) — core engine.

Routes each task to the cheapest intelligence tier capable of handling it:
  Tier 0 - deterministic tools
  Tier 1 - local LLM
  Tier 2 - frontier LLM (e.g. Claude)
"""

__version__ = "0.1.0"
