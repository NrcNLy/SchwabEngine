---
description: Automatically prepares a timestamped research context snapshot document when requested.
globs: 
---

# Cursor Rule: Prep Research Context

Trigger: Whenever asked to "prep research context", "generate research context", or prepare baseline documentation for an advisory session:

1. Run:
   ```bash
   python scripts/prep_research.py
   ```
2. The script compiles a comprehensive snapshot into `docs/YYMMDD.HH-research-context.md` and copies to `research_context.md`.
3. Ensures all 4 required sections are present:
   - `# 1. Architecture & Framework`
   - `# 2. Active Algorithms & Risk Models`
   - `# 3. Recent Logs & Telemetry`
   - `# 4. Current Implementation Gaps`
