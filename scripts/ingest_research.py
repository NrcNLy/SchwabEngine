#!/usr/bin/env python3
"""
scripts/ingest_research.py
==========================
Automated research ingestion skill and document compilation pipeline.
Converts incoming architectural and quantitative research .docx files into
standardized GitHub Flavored Markdown (GFM) with strict LaTeX mathematical
preservation, cleans Word/Pandoc formatting artifacts, validates code blocks,
and logs master reference update notifications.

Usage:
    python scripts/ingest_research.py "C:\\path\\to\\document.docx" [--output docs/07-custom-research.md]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_research")

ROOT_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT_DIR / "docs"


def locate_pandoc() -> str:
    """Locates pandoc executable on the system path or common Windows locations."""
    found = shutil.which("pandoc")
    if found:
        return found

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Pandoc" / "pandoc.exe",
        Path("C:/Program Files/Pandoc/pandoc.exe"),
        Path("C:/Program Files (x86)/Pandoc/pandoc.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)

    raise FileNotFoundError(
        "Pandoc executable not found. Please install Pandoc or ensure it is on your PATH."
    )


def clean_markdown_artifacts(markdown_text: str) -> str:
    """
    Cleans Pandoc/Word artifacts from the converted Markdown:
    - Strips `<span class="mark">` and `</span>` wrappers.
    - Strips empty `<span ...>` tags.
    - Normalizes `[<u>...</u>]` to `[...]`.
    - Normalizes non-breaking spaces and trailing backslashes.
    """
    text = markdown_text

    # 1. Clean span tags
    text = re.sub(r'<span class="mark">(.*?)</span>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<span[^>]*>(.*?)</span>', r'\1', text, flags=re.DOTALL)

    # 2. Clean underline tags inside markdown links
    text = re.sub(r'\[<u>(.*?)</u>\]', r'[\1]', text)

    # 3. Fix escaped quote marks or excessive backslashes
    text = re.sub(r'\\([_#*\[\]])', r'\1', text)

    # 4. Normalize trailing line-continuation backslashes at end of lines
    text = re.sub(r'\\\s*\n', '\n', text)

    return text


def inspect_and_verify_markdown(markdown_text: str, filename: str) -> dict:
    """
    Verifies the converted markdown for equations, code fences, and image tags.
    Returns audit metrics.
    """
    display_equations = len(re.findall(r'\$\$', markdown_text)) // 2
    inline_math = len(re.findall(r'(?<!\$)\$(?!\$)[^\$\n]+(?<!\$)\$(?!\$)', markdown_text))
    code_fences = len(re.findall(r'```', markdown_text))
    img_tags = re.findall(r'<img [^>]*src=[\"\']media/([^\"\']+)[\"\'][^>]*>', markdown_text)
    span_tags = len(re.findall(r'<span', markdown_text))

    metrics = {
        "file": filename,
        "total_lines": len(markdown_text.splitlines()),
        "display_equations": display_equations,
        "inline_math_matches": inline_math,
        "code_fences": code_fences,
        "code_fences_balanced": (code_fences % 2 == 0),
        "embedded_media_images": len(img_tags),
        "span_tags_remaining": span_tags,
    }

    logger.info("=== Research Document Quality Audit for '%s' ===", filename)
    logger.info("  Lines: %d", metrics["total_lines"])
    logger.info("  Display Math ($$...$$): %d", metrics["display_equations"])
    logger.info("  Inline Math ($...$): %d", metrics["inline_math_matches"])
    logger.info("  Code Fences (```): %d (Balanced: %s)", metrics["code_fences"], metrics["code_fences_balanced"])
    logger.info("  Embedded Media Images: %d", metrics["embedded_media_images"])
    logger.info("  Remaining HTML Spans: %d", metrics["span_tags_remaining"])

    if not metrics["code_fences_balanced"]:
        logger.warning("[AUDIT WARNING] Unbalanced code fences detected in %s!", filename)

    if metrics["embedded_media_images"] > 0:
        logger.warning(
            "[AUDIT ADVISORY] Document contains %d embedded image tags (<img src=\"media/...\"). "
            "Inspect equation images and transcribe to LaTeX if necessary.",
            metrics["embedded_media_images"]
        )

    return metrics


def ingest_docx(docx_path: Path, output_path: Path | None = None) -> Path:
    """
    Executes the ingestion pipeline for a single .docx file:
    1. Pandoc conversion with GFM target and --wrap=none.
    2. Artifact cleanup.
    3. Metrics audit.
    4. Master reference notification.
    """
    if not docx_path.is_file():
        raise FileNotFoundError(f"Source file not found: {docx_path}")

    pandoc_exe = locate_pandoc()
    logger.info("Using Pandoc at: %s", pandoc_exe)

    if output_path is None:
        # Default destination: docs/<slug>.md
        clean_stem = re.sub(r'^\d+\s*', '', docx_path.stem)
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', clean_stem).strip('-').lower()
        output_path = DOCS_DIR / f"{slug}.md"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Converting '%s' -> '%s'...", docx_path.name, output_path)

    cmd = [
        pandoc_exe,
        "-f", "docx",
        "-t", "gfm",
        "--wrap=none",
        str(docx_path),
        "-o", str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Pandoc conversion failed:\n{result.stderr}")

    # Read converted markdown
    with open(output_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Clean artifacts
    cleaned_content = clean_markdown_artifacts(content)

    # Save cleaned markdown
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(cleaned_content)

    # Run verification audit
    inspect_and_verify_markdown(cleaned_content, output_path.name)

    # Master reference update reminder
    logger.info("================================================================================")
    logger.info("[ACTION REQUIRED] Document ingestion complete: %s", output_path)
    logger.info("  1. Review generated document for LaTeX math fidelity.")
    logger.info("  2. Add document summary to 'docs/00-reference-index.md'.")
    logger.info("  3. Synthesize architectural insights into 'docs/SCHWAB_ENGINE_MASTER_REFERENCE.md'.")
    logger.info("================================================================================")

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Ingest .docx research files into standardized GFM Markdown with LaTeX verification."
    )
    parser.add_argument("docx_path", type=Path, help="Path to input .docx document")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output markdown path in docs/")

    args = parser.parse_args()

    try:
        dest = ingest_docx(args.docx_path, args.output)
        print(f"\nSUCCESS: Ingested '{args.docx_path.name}' to '{dest}'")
    except Exception as exc:
        logger.error("Ingestion failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
