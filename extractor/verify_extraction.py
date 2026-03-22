"""
verify_extraction.py — Stage 1b of the LabVIEW Project Intelligence Scanner.

Reads output/project_manifest.json and prints a summary report.
Exits with code 1 if the failure rate exceeds the configured threshold.

Usage:
  python verify_extraction.py
  python verify_extraction.py --threshold 30
  python verify_extraction.py --config custom_config.yaml
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.config_loader import load_config, get_output_dir


def verify(cfg: dict, threshold_override: float | None = None):
    output_dir = get_output_dir(cfg)
    manifest_path = output_dir / "project_manifest.json"

    if not manifest_path.exists():
        print(f"ERROR: Manifest not found at {manifest_path}")
        print("Run extract_project.py first.")
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    total = len(manifest)
    ok = sum(1 for e in manifest if e.get("status") == "ok")
    errored = sum(1 for e in manifest if e.get("status") == "error")
    opaque = sum(1 for e in manifest if e.get("status") == "opaque")
    external = sum(1 for e in manifest if e.get("status") == "external")

    has_metadata = sum(1 for e in manifest if e.get("has_metadata"))
    has_bd_image = sum(1 for e in manifest if e.get("has_bd_image"))
    has_fp_image = sum(1 for e in manifest if e.get("has_fp_image"))

    threshold = threshold_override if threshold_override is not None else cfg["analysis"]["failure_threshold_pct"]

    # Report
    print("=" * 50)
    print("  LabVIEW Extraction Verification Report")
    print("=" * 50)
    print(f"  Total VIs in project  : {total}")
    print(f"  Successfully extracted: {ok}")
    print(f"  With metadata JSON    : {has_metadata}")
    print(f"  With block diag image : {has_bd_image}")
    print(f"  With front panel image: {has_fp_image}")
    print(f"  Opaque (skipped)      : {opaque}")
    print(f"  External (skipped)    : {external}")
    print(f"  Errors                : {errored}")
    print("=" * 50)

    if errored > 0:
        print("\nErrored VIs:")
        for e in manifest:
            if e.get("status") == "error":
                print(f"  - {e['vi_name']}: {e.get('error', 'unknown error')}")

    # Failure rate: only count actual errors (not opaque/external, which are expected)
    actionable = total - external  # external VIs are always skipped, don't count against
    failure_pct = (errored / actionable * 100) if actionable > 0 else 0.0
    print(f"\nFailure rate: {failure_pct:.1f}% (threshold: {threshold}%)")

    if failure_pct > threshold:
        print(f"\nFAIL: Failure rate {failure_pct:.1f}% exceeds threshold {threshold}%")
        sys.exit(1)
    else:
        print("PASS: Extraction within acceptable failure threshold.")


def main():
    parser = argparse.ArgumentParser(
        description="Verify LabVIEW extraction results from project_manifest.json."
    )
    parser.add_argument("--threshold", type=float, default=None,
                        help="Override failure threshold percentage (default: from config)")
    parser.add_argument("--config", default=None, help="Path to config.yaml (optional)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    verify(cfg, threshold_override=args.threshold)


if __name__ == "__main__":
    main()
