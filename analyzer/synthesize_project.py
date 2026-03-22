"""
synthesize_project.py — Stage 4 of the LabVIEW Project Intelligence Scanner.

Reads all per-VI analysis JSONs and the dependency graph, sends a synthesis
prompt to the local LLM, and produces:
  output/project_report.json   — structured project-level report
  output/project_report.md     — human-readable Markdown version

Usage:
  python synthesize_project.py
  python synthesize_project.py --project-name "My Instrument"
  python synthesize_project.py --config custom_config.yaml
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.config_loader import load_config, get_output_dir, get_subdir

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_all_analyses(analysis_dir: Path) -> list[dict]:
    results = []
    for path in sorted(analysis_dir.glob("*_analysis.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                results.append(json.load(f))
        except Exception as e:
            log.warning("Could not read %s: %s", path.name, e)
    return results


def _find_entry_points(analyses: list[dict], dep_graph: dict) -> list[str]:
    """VIs that are not called by any other VI in the project."""
    all_callees = set()
    for callees in dep_graph.values():
        all_callees.update(callees)

    all_vis = {a["vi_name"] for a in analyses if "vi_name" in a}
    return sorted(all_vis - all_callees)


def _build_synthesis_prompt(project_name: str, analyses: list[dict],
                             dep_graph: dict, entry_points: list[str]) -> str:
    # Trim each VI analysis to essential fields to keep prompt manageable
    trimmed = []
    for a in analyses:
        trimmed.append({
            "vi_name": a.get("vi_name", ""),
            "purpose": a.get("purpose", ""),
            "key_logic_patterns": a.get("key_logic_patterns", []),
            "complexity_estimate": a.get("complexity_estimate", ""),
            "error_handling": a.get("error_handling", False),
            "analysis_confidence": a.get("analysis_confidence", ""),
        })

    return f"""You are a LabVIEW system architect. You have received per-VI analyses for a \
complete LabVIEW project named "{project_name}". Synthesize these into a high-level \
project report.

Output ONLY a valid JSON object conforming exactly to this schema — no markdown, no preamble:
{{
  "project_name": "<string>",
  "summary": "<2-4 sentence description of what the overall project does>",
  "architecture_pattern": "<dominant pattern: state_machine|producer_consumer|event_driven|flat_sequence|hybrid|unknown>",
  "entry_points": ["<vi_name>"],
  "subsystems": [
    {{
      "name": "<subsystem name>",
      "vis": ["<vi_name>"],
      "purpose": "<what this group of VIs does together>"
    }}
  ],
  "statistics": {{
    "total_vi_count": <int>,
    "analyzed_count": <int>,
    "opaque_count": <int>,
    "high_complexity_vis": ["<vi_name>"]
  }},
  "notes": "<any cross-cutting observations, or null>"
}}

Entry points (VIs not called by others): {json.dumps(entry_points)}

VI Analyses:
{json.dumps(trimmed, indent=2)}

Dependency Graph (VI → [callees]):
{json.dumps(dep_graph, indent=2)}

Respond with the project report JSON only."""


def _render_markdown(report: dict) -> str:
    """Convert the project report JSON to a Markdown document."""
    lines = [
        f"# LabVIEW Project Report: {report.get('project_name', 'Unknown')}",
        "",
        "## Summary",
        report.get("summary", ""),
        "",
        f"**Architecture Pattern:** {report.get('architecture_pattern', 'unknown')}",
        "",
        "## Entry Points",
    ]

    for ep in report.get("entry_points", []):
        lines.append(f"- `{ep}`")

    lines += ["", "## Subsystems"]
    for sub in report.get("subsystems", []):
        lines.append(f"\n### {sub.get('name', 'Unnamed')}")
        lines.append(sub.get("purpose", ""))
        lines.append("\nVIs:")
        for v in sub.get("vis", []):
            lines.append(f"- `{v}`")

    stats = report.get("statistics", {})
    lines += [
        "",
        "## Statistics",
        f"- Total VIs: {stats.get('total_vi_count', 0)}",
        f"- Analyzed: {stats.get('analyzed_count', 0)}",
        f"- Opaque/skipped: {stats.get('opaque_count', 0)}",
    ]

    high_complexity = stats.get("high_complexity_vis", [])
    if high_complexity:
        lines += ["", "### High Complexity VIs"]
        for v in high_complexity:
            lines.append(f"- `{v}`")

    lines += ["", "## All VI Analyses"]
    for vi_analysis in report.get("vis", []):
        vi_name = vi_analysis.get("vi_name", "?")
        purpose = vi_analysis.get("purpose", "")
        confidence = vi_analysis.get("analysis_confidence", "")
        patterns = ", ".join(vi_analysis.get("key_logic_patterns", []))
        lines += [
            f"\n### `{vi_name}`",
            f"**Purpose:** {purpose}",
            f"**Confidence:** {confidence}",
        ]
        if patterns:
            lines.append(f"**Patterns:** {patterns}")
        if vi_analysis.get("error_handling"):
            lines.append("**Error Handling:** Yes")
        notes = vi_analysis.get("notes")
        if notes:
            lines.append(f"**Notes:** {notes}")

    notes = report.get("notes")
    if notes:
        lines += ["", "## Additional Notes", notes]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_synthesis(project_name: str, cfg: dict):
    from openai import OpenAI

    output_dir = get_output_dir(cfg)
    analysis_dir = get_subdir(cfg, "analysis")

    dep_graph_path = output_dir / "dependency_graph.json"
    if not dep_graph_path.exists():
        log.error("dependency_graph.json not found. Run extract_project.py first.")
        sys.exit(1)

    with open(dep_graph_path, "r", encoding="utf-8") as f:
        dep_graph = json.load(f)

    analyses = _load_all_analyses(analysis_dir)
    if not analyses:
        log.error("No analysis files found in %s. Run analyze_vi.py first.", analysis_dir)
        sys.exit(1)

    log.info("Loaded %d VI analyses", len(analyses))

    # Compute statistics
    manifest_path = output_dir / "project_manifest.json"
    opaque_count = 0
    total_vi_count = len(analyses)
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        total_vi_count = len(manifest)
        opaque_count = sum(1 for e in manifest if e.get("opaque"))

    high_complexity = [
        a["vi_name"] for a in analyses
        if a.get("complexity_estimate") == "high" and "vi_name" in a
    ]

    entry_points = _find_entry_points(analyses, dep_graph)
    log.info("Entry points: %s", entry_points)

    prompt = _build_synthesis_prompt(project_name, analyses, dep_graph, entry_points)

    client = OpenAI(
        base_url=cfg["llm"]["base_url"],
        api_key="ollama",  # Ollama/LM Studio ignore the value but SDK requires non-empty
    )

    log.info("Sending synthesis prompt to LLM (%s)...", cfg["llm"]["model"])
    try:
        kwargs = {
            "model": cfg["llm"]["model"],
            "messages": [{"role": "user", "content": prompt}],
            "timeout": cfg["llm"]["timeout_seconds"],
        }
        if cfg["llm"]["force_json"]:
            kwargs["response_format"] = {"type": "json_object"}

        response = client.chat.completions.create(**kwargs)
        raw = response.choices[0].message.content
    except Exception as e:
        log.error("LLM synthesis call failed: %s", e)
        sys.exit(1)

    # Parse response
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        report_core = json.loads(text)
    except json.JSONDecodeError as e:
        log.error("Failed to parse LLM synthesis response as JSON: %s", e)
        log.error("Raw response saved to output/synthesis_raw.txt")
        (output_dir / "synthesis_raw.txt").write_text(raw, encoding="utf-8")
        sys.exit(1)

    # Merge in computed fields
    report = {
        **report_core,
        "vis": analyses,
        "dependency_graph": dep_graph,
    }
    if "statistics" not in report:
        report["statistics"] = {}
    report["statistics"].update({
        "total_vi_count": total_vi_count,
        "analyzed_count": len(analyses),
        "opaque_count": opaque_count,
        "high_complexity_vis": high_complexity,
    })

    # Write outputs
    report_json_path = output_dir / "project_report.json"
    report_md_path = output_dir / "project_report.md"

    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    log.info("Written: %s", report_json_path)

    markdown = _render_markdown(report)
    report_md_path.write_text(markdown, encoding="utf-8")
    log.info("Written: %s", report_md_path)


def main():
    parser = argparse.ArgumentParser(
        description="Synthesize per-VI analyses into a project-level report."
    )
    parser.add_argument("--project-name", default="LabVIEW Project",
                        help="Human-readable project name for the report")
    parser.add_argument("--config", default=None, help="Path to config.yaml (optional)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run_synthesis(args.project_name, cfg)


if __name__ == "__main__":
    main()
