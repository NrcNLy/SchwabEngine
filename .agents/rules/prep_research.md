# Antigravity Skill / Rule: Prep Research Context

When the user requests "prep research context", "prep research context for [issue]", or asks for an updated LLM advisory reference document:

1. **Automation Command**:
   Execute the automated research prep script:
   ```bash
   python scripts/prep_research.py [--issue "Issue description"]
   ```
   This extracts the current architecture, mathematical models, incident logs, and implementation gaps, outputting:
   - `docs/YYMMDD.HH-research-context.md` (e.g. `docs/261005.13-research-context.md`)
   - `research_context.md` (root reference copy)

2. **Core Sections Enforced**:
   - `# 1. Architecture & Framework`: T+1 settlement invariants (I1–I5), GFV elimination, 20% single-ticker exposure cap, 3,500 daily quota rate limiter, and auth circuit breaker.
   - `# 2. Active Algorithms & Risk Models`: Exact mathematical equations for Yang-Zhang volatility, stop distance calculation, monotonic ratcheting, Tier-1 (in-memory) vs. Tier-2 (broker catastrophe) stop rules, and Bayesian Quarter-Kelly sizing.
   - `# 3. Recent Logs & Telemetry`: Session incident logs covering 401 Unauthorized API lockouts, order message volume, and trailing stop triggers.
   - `# 4. Current Implementation Gaps`: Missing Hurst Exponent filter, static 1D Bayesian prior, microstructure shadow-mode decoupling on exits, and intraday time-scaling discrepancies.
