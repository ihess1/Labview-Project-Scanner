"""
analyze_vi.py — Stage 3 of the LabVIEW Project Intelligence Scanner.

Reads per-VI metadata JSON (and optionally block-diagram images) from the
output/ directory, sends structured prompts to a local LLM, and writes
per-VI analysis JSON to output/analysis/.

Usage:
  python analyze_vi.py MyVI                     # analyze one VI by name
  python analyze_vi.py --all                    # analyze all VIs in manifest
  python analyze_vi.py --all --vision           # include block diagram images
  python analyze_vi.py --all --vision --config custom_config.yaml
"""

import argparse
import asyncio
import base64
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.config_loader import load_config, get_output_dir, get_subdir, sanitize_vi_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent
_PROMPT_PATH = _REPO_ROOT / "prompts" / "vi_analysis_prompt.txt"


# ---------------------------------------------------------------------------
# Prompt loading and rendering
# ---------------------------------------------------------------------------

def _load_prompt_template() -> tuple[str, str]:
    """Parse vi_analysis_prompt.txt and return (system_prompt, user_prompt_template)."""
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    system_marker = "SYSTEM:"
    user_marker = "USER:"

    sys_start = text.find(system_marker)
    user_start = text.find(user_marker)

    if sys_start == -1 or user_start == -1:
        raise ValueError(f"Prompt file must contain both '{system_marker}' and '{user_marker}' sections.")

    system_prompt = text[sys_start + len(system_marker):user_start].strip()
    user_template = text[user_start + len(user_marker):].strip()
    return system_prompt, user_template


def _render_user_prompt(template: str, metadata: dict, use_vision: bool) -> str:
    vision_line = "Block diagram image attached." if use_vision else ""
    prompt = template.replace("{{metadata_json}}", json.dumps(metadata, indent=2))
    prompt = prompt.replace("{{vision_line}}", vision_line)
    return prompt


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _build_messages(system_prompt: str, user_prompt: str,
                    image_path: Path | None, use_vision: bool) -> list[dict]:
    """Build the messages list for the OpenAI-compatible API."""
    messages = [{"role": "system", "content": system_prompt}]

    if use_vision and image_path and image_path.exists():
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        user_content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            },
            {"type": "text", "text": user_prompt},
        ]
    else:
        user_content = user_prompt

    messages.append({"role": "user", "content": user_content})
    return messages


async def _call_llm(client, model: str, messages: list[dict],
                    timeout: int, force_json: bool) -> str:
    """Call the LLM and return the response text."""
    kwargs = {
        "model": model,
        "messages": messages,
        "timeout": timeout,
    }
    if force_json:
        kwargs["response_format"] = {"type": "json_object"}

    response = await client.chat.completions.create(**kwargs)
    return response.choices[0].message.content


def _parse_json_response(raw: str, vi_name: str) -> dict:
    """Parse LLM response as JSON, with graceful fallback."""
    # Strip markdown fences if the model ignored instructions
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first and last fence lines
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("JSON parse failed for %s: %s", vi_name, e)
        return {
            "vi_name": vi_name,
            "error": "json_parse_failed",
            "raw_response": raw,
            "analysis_confidence": "low",
        }


# ---------------------------------------------------------------------------
# Per-VI analysis coroutine
# ---------------------------------------------------------------------------

async def analyze_vi(
    vi_name: str,
    cfg: dict,
    semaphore: asyncio.Semaphore,
    use_vision: bool,
    system_prompt: str,
    user_template: str,
    client,
):
    """Analyze a single VI and write its analysis JSON."""
    async with semaphore:
        meta_dir = get_subdir(cfg, "metadata")
        img_dir = get_subdir(cfg, "images")
        analysis_dir = get_subdir(cfg, "analysis")

        safe_name = sanitize_vi_name(vi_name)
        metadata_path = meta_dir / f"{safe_name}.json"
        bd_image_path = img_dir / f"{safe_name}_bd.png"
        analysis_path = analysis_dir / f"{safe_name}_analysis.json"

        if not metadata_path.exists():
            log.warning("Metadata not found for %s, skipping", vi_name)
            return

        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        user_prompt = _render_user_prompt(user_template, metadata, use_vision)
        model = cfg["llm"]["vision_model"] if use_vision else cfg["llm"]["model"]
        messages = _build_messages(system_prompt, user_prompt,
                                   bd_image_path if use_vision else None,
                                   use_vision)

        try:
            raw = await _call_llm(
                client, model, messages,
                timeout=cfg["llm"]["timeout_seconds"],
                force_json=cfg["llm"]["force_json"],
            )
            result = _parse_json_response(raw, vi_name)
            log.info("Analyzed: %s (confidence=%s)", vi_name,
                     result.get("analysis_confidence", "?"))
        except Exception as e:
            log.warning("LLM call failed for %s: %s", vi_name, e)
            result = {
                "vi_name": vi_name,
                "error": str(e),
                "analysis_confidence": "low",
            }

        with open(analysis_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_analysis(vi_names: list[str], cfg: dict, use_vision: bool):
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=cfg["llm"]["base_url"],
        api_key="ollama",  # Ollama/LM Studio ignore the value but SDK requires non-empty
    )

    system_prompt, user_template = _load_prompt_template()
    semaphore = asyncio.Semaphore(cfg["llm"]["max_concurrency"])

    tasks = [
        analyze_vi(name, cfg, semaphore, use_vision, system_prompt, user_template, client)
        for name in vi_names
    ]
    log.info("Analyzing %d VIs (concurrency=%d, vision=%s)...",
             len(tasks), cfg["llm"]["max_concurrency"], use_vision)
    await asyncio.gather(*tasks)
    log.info("Analysis complete. Results in output/analysis/")


def _get_vi_names_from_manifest(cfg: dict, skip_opaque: bool) -> list[str]:
    output_dir = get_output_dir(cfg)
    manifest_path = output_dir / "project_manifest.json"
    if not manifest_path.exists():
        log.error("project_manifest.json not found. Run extract_project.py first.")
        sys.exit(1)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    names = []
    for entry in manifest:
        if entry.get("status") != "ok":
            continue
        if skip_opaque and entry.get("opaque"):
            continue
        names.append(entry["vi_name"])
    return names


def main():
    parser = argparse.ArgumentParser(
        description="Analyze LabVIEW VIs using a local LLM."
    )
    parser.add_argument("vi_name", nargs="?", help="Name of a single VI to analyze")
    parser.add_argument("--all", action="store_true", help="Analyze all VIs in manifest")
    parser.add_argument("--vision", action="store_true",
                        help="Include block diagram images (requires vision model)")
    parser.add_argument("--config", default=None, help="Path to config.yaml (optional)")
    args = parser.parse_args()

    if not args.vi_name and not args.all:
        parser.error("Provide a VI name or use --all")

    cfg = load_config(args.config)

    if args.all:
        vi_names = _get_vi_names_from_manifest(cfg, skip_opaque=cfg["analysis"]["skip_opaque"])
    else:
        vi_names = [args.vi_name]

    if not vi_names:
        log.warning("No VIs to analyze.")
        sys.exit(0)

    # Windows asyncio fix for Python 3.8+
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(run_analysis(vi_names, cfg, use_vision=args.vision))


if __name__ == "__main__":
    main()
