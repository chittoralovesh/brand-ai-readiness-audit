"""Deterministic, standard-library-only audit engine for the brand AI-readiness marketplace.

The library is owned by the `audit-orchestrator` skill. It provides the shared,
side-effect-free primitives that the orchestrator uses to build a *site snapshot*:
URL normalisation, robots.txt policy, a polite bounded HTTP client, a tolerant HTML
extractor and the crawl frontier.

Analyzer skills (`crawl-render-audit`, `ai-citability-audit`,
`freshness-corroboration`, `engagement-audit`) deliberately do NOT import this
package. They consume the JSON snapshot instead, which keeps them decoupled,
independently runnable and cheap to reason about.
"""

__version__ = "1.0.0"
