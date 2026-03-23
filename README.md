# LabVIEW Project Intelligence Scanner

A four-stage Python pipeline that connects to a running LabVIEW instance,
extracts structured metadata and block-diagram images from every VI in a
`.lvproj` file, and uses a local LLM (via Ollama or LM Studio) to produce
a per-VI analysis and a human-readable project-level report.

```
.lvproj → extract → verify → analyze (LLM) → synthesize → project_report.md
```

---

## Requirements

| Requirement | Details |
|---|---|
| **OS** | Windows only (COM/VI Server automation) |
| **LabVIEW** | 2018+ running with VI Server enabled; 2024 recommended |
| **Python** | 3.10+ |
| **Local LLM server** | [Ollama](https://ollama.com) or LM Studio with an OpenAI-compatible endpoint |

### Python dependencies

```
pip install -r requirements.txt
```

`requirements.txt` installs: `pywin32`, `openai`, `pyyaml`.

---

## LabVIEW Setup

Before running the scanner, LabVIEW must be configured to allow COM access:

1. Open LabVIEW and go to **Tools → Options → VI Server**.
2. Under **Protocols**, enable **TCP/IP** and note the port (default `3363`).
3. Under **Machine Access**, allow local connections (or add `localhost`).
4. Click **OK** and leave LabVIEW running — do **not** open the project manually
   beforehand; the scanner opens VIs itself via COM.

---

## LLM Setup

The scanner expects an OpenAI-compatible HTTP endpoint.

### Ollama (recommended)

```bash
# Install Ollama, then pull the models you want:
ollama pull qwen2.5-coder:32b      # text-only analysis model
ollama pull qwen2-vl:72b           # optional vision model

# Ollama serves on http://localhost:11434/v1 by default
```

### LM Studio

Start the local server from **Local Server** tab; set the endpoint in
`config.yaml` (e.g. `http://localhost:1234/v1`).

Any model that supports the OpenAI chat completions API and JSON mode
(`response_format: json_object`) works.

---

## Configuration

Copy and edit `config.yaml` before running:

```yaml
labview:
  vi_server_port: 3363     # LabVIEW VI Server port
  export_images: true      # set false to skip PNG export (faster, no vision)
  image_format: png

llm:
  base_url: "http://localhost:11434/v1"   # Ollama default
  model: "qwen2.5-coder:32b"             # text-only model for VI analysis + synthesis
  vision_model: "qwen2-vl:72b"           # only used when --vision flag is passed
  timeout_seconds: 120                   # per-request timeout
  max_concurrency: 4                     # parallel LLM calls during analysis stage
  force_json: true                       # pass response_format=json_object to the API

paths:
  output_dir: "./output"   # all outputs land here

analysis:
  use_vision: true                  # enables vision in analyze_vi.py when --vision is passed
  skip_opaque: true                 # skip packed libraries and XControls
  complexity_threshold_nodes: 50   # VIs above this node count are flagged as high-complexity
  failure_threshold_pct: 20        # verify stage fails if more than this % of VIs errored
```

All fields have built-in defaults; the file is optional if you accept the defaults.

---

## Quick Start

```bash
# Full pipeline (extract + verify + analyze + synthesize):
python run_pipeline.py --project-file "C:\Projects\MyInstrument\MyInstrument.lvproj"

# Include block diagram images in analysis (requires a vision model):
python run_pipeline.py --project-file "C:\Projects\MyInstrument\MyInstrument.lvproj" --vision

# Give the report a readable title:
python run_pipeline.py --project-file "C:\Projects\MyInstrument\MyInstrument.lvproj" \
    --project-name "My Instrument Control System"
```

The pipeline prints progress for each stage and aborts on the first failure.
Final outputs are printed to the console when complete.

---

## Pipeline Stages

### Stage 1 — Extract (`extractor/extract_project.py`)

Connects to the running LabVIEW instance via COM, parses the `.lvproj` XML,
and for each VI:

- Reads description, connector pane pattern, reentrant flag.
- Enumerates front-panel controls/indicators (inputs and outputs with data types).
- Counts block-diagram nodes and identifies diagram structures (While Loop,
  Case Structure, Event Structure, etc.) and called sub-VIs.
- Exports the block diagram and front panel as PNG images.

**Writes:**
```
output/
  metadata/          ← one <vi_name>.json per VI
  images/            ← <vi_name>_bd.png and <vi_name>_fp.png
  dependency_graph.json
  project_manifest.json
```

VIs that cannot be accessed (e.g. corrupted files, broken paths) are recorded
in the manifest with `"status": "error"` and extraction continues.

Packed libraries (`*.lvlibp`), XControls, and LabVIEW built-in VIs are marked
`"opaque"` or `"external"` and skipped automatically.

### Stage 1b — Verify (`extractor/verify_extraction.py`)

Reads `project_manifest.json` and checks that the failure rate is below
`failure_threshold_pct` (default 20%). Exits non-zero if the threshold is
exceeded, stopping the pipeline early with a clear message.

### Stage 2 — Analyze (`analyzer/analyze_vi.py`)

For each successfully extracted VI, sends a structured prompt (from
`prompts/vi_analysis_prompt.txt`) to the local LLM. Requests are batched
with `max_concurrency` parallel asyncio tasks.

The prompt includes all metadata extracted in Stage 1. With `--vision`, the
block diagram PNG is base64-encoded and sent as an image content block using
the OpenAI vision message format.

**Writes:**
```
output/analysis/<vi_name>_analysis.json
```

Each analysis JSON contains:
- `purpose` — one-sentence functional description
- `inputs` / `outputs` — terminals with inferred functional roles
- `key_logic_patterns` — e.g. `state_machine`, `producer_consumer`
- `complexity_estimate` — `low` / `medium` / `high`
- `error_handling` — `true` / `false`
- `analysis_confidence` — `low` / `medium` / `high`
- `notes` — architectural observations

### Stage 3 — Synthesize (`analyzer/synthesize_project.py`)

Aggregates all per-VI analyses into a single project-level prompt and asks the
LLM to produce a structured project report.

**Writes:**
```
output/project_report.json   ← machine-readable full report
output/project_report.md     ← human-readable Markdown
```

The report includes:
- Project summary (2–4 sentences)
- Dominant architecture pattern (state machine, producer-consumer, etc.)
- Entry points (VIs not called by any other VI)
- Subsystem groupings
- Statistics: total VIs, analyzed, opaque, high-complexity list
- Full dependency graph

---

## Running Individual Stages

Each script can be run independently, which is useful for re-running just one
stage without repeating the others.

```bash
# Re-run only the LLM analysis (e.g. after changing config.yaml model):
python run_pipeline.py --project-file path/to/project.lvproj --skip-extraction

# Re-run only synthesis (e.g. after tweaking the synthesis prompt):
python run_pipeline.py --project-file path/to/project.lvproj \
    --skip-extraction --skip-analysis --project-name "My Project"

# Analyze a single VI by name:
python analyzer/analyze_vi.py "DataAcquisition.vi"

# Analyze all VIs with vision:
python analyzer/analyze_vi.py --all --vision

# Run extraction only (useful for debugging COM issues):
python extractor/extract_project.py "C:\Projects\MyInstrument.lvproj"

# Check extraction results:
python extractor/verify_extraction.py
```

---

## Output Structure

```
output/
├── project_manifest.json        # All VIs with status flags
├── dependency_graph.json        # VI → [called sub-VIs] map
├── project_report.json          # Full project report (machine-readable)
├── project_report.md            # Full project report (human-readable)
├── metadata/
│   ├── Main.vi.json
│   ├── DataAcquisition.vi.json
│   └── ...
├── images/
│   ├── Main.vi_bd.png           # Block diagram
│   ├── Main.vi_fp.png           # Front panel
│   └── ...
└── analysis/
    ├── Main.vi_analysis.json
    ├── DataAcquisition.vi_analysis.json
    └── ...
```

The `output/` directory is gitignored.

---

## Troubleshooting

### "Failed to connect to LabVIEW"

- Make sure LabVIEW is running before starting the scanner.
- Confirm VI Server is enabled: **Tools → Options → VI Server → TCP/IP**.
- Confirm the port in `config.yaml` matches the VI Server port.
- Run the script as the same Windows user that launched LabVIEW (COM uses DCOM
  and is user-session-bound).

### "ExportImage failed" warnings

LabVIEW's COM `ExportImage` API varies slightly across versions. The scanner
tries the 3-argument form (LabVIEW 2024) then falls back to the 2-argument form
(LabVIEW 2018–2023). If both fail, the VI is still analyzed using text metadata
only; the warning is non-fatal.

### High failure rate / verify stage exits 1

Check `output/project_manifest.json` for entries with `"status": "error"` and
read the `"error"` field for details. Common causes:

- VI file not found at the path stored in the `.lvproj` (moved/renamed files).
- VI is password-protected.
- LabVIEW is in a bad state — try saving and re-opening the project.

Lower the threshold in `config.yaml` if you want to proceed despite errors:

```yaml
analysis:
  failure_threshold_pct: 50   # allow up to 50% failures
```

### LLM returns non-JSON

The scanner has a multi-layer JSON fallback:

1. Tries `json.loads()` directly.
2. Strips markdown fences (` ```json ... ``` `).
3. On failure: writes `{ "error": "json_parse_failed", "raw_response": "..." }`
   and continues.

If this happens often, switch to a model with stronger instruction-following,
or ensure your Ollama/LM Studio version supports `response_format: json_object`.

### LLM times out

Increase `timeout_seconds` in `config.yaml` or reduce `max_concurrency` to
avoid overloading the local GPU.

### "Module not found" when running a script directly

Run scripts from the repo root directory, or use the top-level orchestrator:

```bash
# From the repo root:
cd C:\path\to\Labview-Project-Scanner
python run_pipeline.py ...
```

---

## Architecture Notes

- **Extraction is synchronous** — COM automation must run on a single thread;
  `pythoncom.CoInitialize()` is called once at startup.
- **Analysis is async** — `analyze_vi.py` uses `asyncio.gather` with a
  `Semaphore` to batch parallel LLM calls without overloading the server.
- **Synthesis is synchronous** — a single blocking call; the synthesizer
  compresses VI analyses to ~100 tokens each before sending to avoid exceeding
  the model's context window.
- **No external state** — all inter-stage communication happens via files in
  `output/`. Stages can be run independently or debugged in isolation.
