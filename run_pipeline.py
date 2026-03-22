"""
run_pipeline.py — Top-level orchestration for the LabVIEW Project Intelligence Scanner.

Runs all four pipeline stages in sequence with progress reporting:
  Stage 1: extract_project.py   — extract metadata + images from LabVIEW
  Stage 1b: verify_extraction.py — sanity-check extraction results
  Stage 2: analyze_vi.py        — per-VI LLM analysis
  Stage 3: synthesize_project.py — aggregate into project report

Usage:
  python run_pipeline.py --project-file path/to/project.lvproj
  python run_pipeline.py --project-file path/to/project.lvproj --vision
  python run_pipeline.py --skip-extraction --project-name "My Instrument"
  python run_pipeline.py --skip-extraction --skip-analysis
"""

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent


def _run(args: list[str], stage_name: str) -> int:
    """Run a subprocess command and return its exit code."""
    print(f"\n{'='*60}")
    print(f"  STAGE: {stage_name}")
    print(f"{'='*60}")
    print(f"  Command: {' '.join(args)}\n")

    result = subprocess.run(args, cwd=str(_REPO_ROOT))
    if result.returncode != 0:
        print(f"\n  ERROR: Stage '{stage_name}' failed with exit code {result.returncode}")
    return result.returncode


def main():
    parser = argparse.ArgumentParser(
        description="Run the full LabVIEW Project Intelligence Scanner pipeline."
    )
    parser.add_argument("--project-file", default=None,
                        help="Path to the .lvproj file (required unless --skip-extraction)")
    parser.add_argument("--project-name", default="LabVIEW Project",
                        help="Human-readable project name for the report")
    parser.add_argument("--vision", action="store_true",
                        help="Use vision model for block diagram images in analysis")
    parser.add_argument("--skip-extraction", action="store_true",
                        help="Skip extraction stage (use existing output/)")
    parser.add_argument("--skip-analysis", action="store_true",
                        help="Skip analysis stage (only re-run synthesis)")
    parser.add_argument("--config", default=None,
                        help="Path to config.yaml (optional)")
    args = parser.parse_args()

    python = sys.executable
    config_args = ["--config", args.config] if args.config else []

    # -----------------------------------------------------------------------
    # Stage 1: Extract
    # -----------------------------------------------------------------------
    if not args.skip_extraction:
        if not args.project_file:
            parser.error("--project-file is required unless --skip-extraction is set")

        rc = _run(
            [python, str(_REPO_ROOT / "extractor" / "extract_project.py"),
             args.project_file] + config_args,
            "1 — Extract project (VIs, metadata, images)",
        )
        if rc != 0:
            print("\nPipeline aborted at extraction stage.")
            sys.exit(rc)

        # Stage 1b: Verify
        rc = _run(
            [python, str(_REPO_ROOT / "extractor" / "verify_extraction.py")] + config_args,
            "1b — Verify extraction results",
        )
        if rc != 0:
            print("\nPipeline aborted: too many extraction failures.")
            print("Fix the errors or lower the failure threshold in config.yaml.")
            sys.exit(rc)
    else:
        print("\n[Skipping extraction — using existing output/]")

    # -----------------------------------------------------------------------
    # Stage 2: Analyze
    # -----------------------------------------------------------------------
    if not args.skip_analysis:
        vision_args = ["--vision"] if args.vision else []
        rc = _run(
            [python, str(_REPO_ROOT / "analyzer" / "analyze_vi.py"),
             "--all"] + vision_args + config_args,
            "2 — Analyze VIs with local LLM",
        )
        if rc != 0:
            print("\nPipeline aborted at analysis stage.")
            sys.exit(rc)
    else:
        print("\n[Skipping analysis — using existing output/analysis/]")

    # -----------------------------------------------------------------------
    # Stage 3: Synthesize
    # -----------------------------------------------------------------------
    rc = _run(
        [python, str(_REPO_ROOT / "analyzer" / "synthesize_project.py"),
         "--project-name", args.project_name] + config_args,
        "3 — Synthesize project report",
    )
    if rc != 0:
        print("\nPipeline aborted at synthesis stage.")
        sys.exit(rc)

    print(f"\n{'='*60}")
    print("  PIPELINE COMPLETE")
    print(f"{'='*60}")
    print("  Results:")
    print("    output/project_report.json")
    print("    output/project_report.md")
    print("    output/metadata/          (per-VI metadata)")
    print("    output/analysis/          (per-VI LLM analysis)")
    print("    output/images/            (block diagram + front panel PNGs)")


if __name__ == "__main__":
    main()
