#!/usr/bin/env python3
"""
scripts/tools/prep_argon_payload.py
===================================
Organizes project documentation and verifies the asset bundle for Gemini Argon.

Requirements executed:
1. File Consolidation:
   - Locates `SchwabEngine Mathematical Architecture.md` (including Drive/local paths).
   - Verifies `data/fixtures/schwab_telemetry_micro_sequence.json` exists in the local workspace.
   - Automatically executes `scripts/dev/generate_mock_stream.py` if telemetry is missing.
2. Canonical Placement:
   - Ensures the math document lives at `docs/architecture/mathematical_architecture.md`
     and the telemetry fixture at `data/fixtures/`.
3. Path Verification:
   - Verifies file integrity, byte size, and outputs exact local file paths.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("prep_argon_payload")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE_DOCS_DIR = PROJECT_ROOT / "docs" / "architecture"
MATH_DOC_PATH = ARCHITECTURE_DOCS_DIR / "mathematical_architecture.md"
TELEMETRY_FIXTURE_PATH = PROJECT_ROOT / "data" / "fixtures" / "schwab_telemetry_micro_sequence.json"

CANDIDATE_SEARCH_PATHS: List[Path] = [
    MATH_DOC_PATH,
    PROJECT_ROOT / "docs" / "SchwabEngine Mathematical Architecture.md",
    Path("G:/My Drive/SchwabEngine Mathematical Architecture (1).md"),
    Path("G:/My Drive/SchwabEngine Mathematical Architecture.md"),
    Path("G:/My Drive/Projects/schwab_engine_research_261015/SchwabEngine Mathematical Architecture.md"),
    Path("C:/Users/nicho/Downloads/SchwabEngine Mathematical Architecture.md"),
    Path("C:/Users/nicho/Downloads/SchwabEngine Mathematical Architecture (1).md"),
]


def find_mathematical_architecture_doc() -> Optional[Path]:
    """Scans candidate locations and Drive mounts for the architecture document."""
    for path in CANDIDATE_SEARCH_PATHS:
        if path.exists() and path.is_file():
            logger.info("Found Mathematical Architecture document: %s (%d bytes)", path, path.stat().st_size)
            return path

    # Fallback globbing on G:\My Drive if available
    gdrive_root = Path("G:/My Drive")
    if gdrive_root.exists():
        matches = list(gdrive_root.glob("*Mathematical Architecture*.md"))
        if matches:
            logger.info("Found document via Drive glob: %s", matches[0])
            return matches[0]

    return None


def verify_or_generate_telemetry(telemetry_path: Path) -> Path:
    """Verifies existence of telemetry sequence, regenerating via script if missing."""
    if telemetry_path.exists() and telemetry_path.stat().st_size > 0:
        logger.info("Verified telemetry micro-sequence: %s (%d bytes)", telemetry_path, telemetry_path.stat().st_size)
        return telemetry_path

    logger.warning("Telemetry file %s missing or empty. Executing scripts/dev/generate_mock_stream.py...", telemetry_path)
    gen_script = PROJECT_ROOT / "scripts" / "dev" / "generate_mock_stream.py"
    if not gen_script.exists():
        raise FileNotFoundError(f"Mock stream generator script not found at {gen_script}")

    result = subprocess.run([sys.executable, str(gen_script)], cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Failed to generate mock stream: %s\n%s", result.stdout, result.stderr)
        raise RuntimeError("Mock stream generation script failed.")

    logger.info("Successfully executed mock stream generator.")
    if not telemetry_path.exists():
        raise FileNotFoundError(f"Expected telemetry file at {telemetry_path} was not created.")

    return telemetry_path


def main() -> None:
    print("\n" + "=" * 80)
    print(" GEMINI ARGON PAYLOAD PREPARATION & ASSET VERIFICATION")
    print("=" * 80)

    # 1. Locate Mathematical Architecture document
    math_doc_source = find_mathematical_architecture_doc()
    if not math_doc_source:
        print(" [ERROR] Unable to locate 'SchwabEngine Mathematical Architecture.md'.")
        sys.exit(1)

    # 2. Verify or generate Telemetry file
    telemetry_root_path = TELEMETRY_FIXTURE_PATH
    telemetry_root_path.parent.mkdir(parents=True, exist_ok=True)
    verify_or_generate_telemetry(telemetry_root_path)

    # 3. Organize at the canonical docs/architecture location
    ARCHITECTURE_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    root_math_path = MATH_DOC_PATH
    doc_math_path = MATH_DOC_PATH

    if math_doc_source.resolve() != root_math_path.resolve():
        shutil.copy2(math_doc_source, root_math_path)
        logger.info("Copied math doc -> %s", root_math_path)

    doc_telemetry_path = telemetry_root_path

    # Quick sanity check on telemetry content
    with open(root_math_path, "r", encoding="utf-8") as f:
        math_lines = len(f.readlines())

    with open(telemetry_root_path, "r", encoding="utf-8") as f:
        telemetry_data = json.load(f)
        event_count = len(telemetry_data)

    print("-" * 80)
    print(" ASSET VERIFICATION RESULTS:")
    print(f" [PASS] 1. Mathematical Architecture Document:")
    print(f"        - Lines:          {math_lines}")
    print(f"        - Size:           {root_math_path.stat().st_size:,} bytes")
    print(f"        - Root Path:      {root_math_path.resolve()}")
    print(f"        - Doc Folder:     {doc_math_path.resolve()}")
    print(f" [PASS] 2. Telemetry Micro-Sequence:")
    print(f"        - Events:         {event_count} events (10s window)")
    print(f"        - Size:           {telemetry_root_path.stat().st_size:,} bytes")
    print(f"        - Root Path:      {telemetry_root_path.resolve()}")
    print(f"        - Doc Folder:     {doc_telemetry_path.resolve()}")
    print("-" * 80)
    print(" EXACT VERIFIED PATHS FOR GEMINI ARGON ATTACHMENT:")
    print(f" 1. {root_math_path.resolve()}")
    print(f" 2. {telemetry_root_path.resolve()}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
