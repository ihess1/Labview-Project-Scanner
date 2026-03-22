"""
extract_project.py — Stage 1 of the LabVIEW Project Intelligence Scanner.

Connects to a running LabVIEW 2024+ instance via COM/VI Server, iterates every
VI in a .lvproj file, extracts structured metadata and block-diagram/front-panel
images, and writes:
  output/metadata/<vi_name>.json     — per-VI metadata
  output/images/<vi_name>_bd.png     — block diagram image
  output/images/<vi_name>_fp.png     — front panel image
  output/dependency_graph.json       — VI → [callees] map
  output/project_manifest.json       — full VI list with status flags

Usage:
  python extract_project.py <path_to_project.lvproj>
  python extract_project.py <path_to_project.lvproj> --config custom_config.yaml
"""

import argparse
import json
import logging
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

# Allow running from the repo root or from the extractor/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.config_loader import load_config, get_subdir, sanitize_vi_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# External URL prefixes that indicate NI built-in VIs — skip COM extraction
EXTERNAL_PREFIXES = ("<vilib>", "<userlib>", "<instrlib>", "<resource>")


# ---------------------------------------------------------------------------
# .lvproj / .lvlib XML parsing
# ---------------------------------------------------------------------------

def _resolve_url(url: str, base_dir: Path) -> Path | None:
    """Resolve a .lvproj item URL to an absolute Windows path.

    Returns None if the URL points to an external/built-in location.
    """
    decoded = urllib.parse.unquote(url)
    for prefix in EXTERNAL_PREFIXES:
        if decoded.startswith(prefix):
            return None
    # Strip any remaining angle-bracket tokens (e.g. <ProjectDir>)
    # NI uses <ProjectDir>/../foo.vi  →  resolve relative to project dir
    decoded = decoded.replace("<ProjectDir>/", "")
    decoded = decoded.replace("<ProjectDir>\\", "")
    decoded = decoded.lstrip("/\\")
    return (base_dir / decoded).resolve()


def _collect_items_from_xml(xml_path: Path, base_dir: Path, depth: int = 0):
    """Recursively yield (vi_abs_path, is_top_level, is_opaque) for each VI item.

    depth=0 means items directly under a Target (top-level).
    """
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        log.warning("Failed to parse XML %s: %s", xml_path, e)
        return

    root = tree.getroot()

    # .lvproj has a <Project> root; .lvlib has a <Library> root.
    # In both cases we want to walk all <Item> descendants.
    def walk(node, current_depth):
        for item in node:
            if item.tag != "Item":
                continue
            item_type = item.get("Type", "")
            url = item.get("URL", "")
            name = item.get("Name", "")

            if item_type == "VI":
                abs_path = _resolve_url(url, base_dir) if url else None
                is_external = abs_path is None
                yield (abs_path, current_depth == 0, False, name, is_external)

            elif item_type in ("Folder", "VirtualFolder"):
                # Recurse; items inside folders are not top-level
                yield from walk(item, current_depth + 1)

            elif item_type == "Library":
                abs_lib = _resolve_url(url, base_dir) if url else None
                if abs_lib and abs_lib.exists():
                    yield from _collect_items_from_xml(abs_lib, abs_lib.parent, current_depth + 1)

            elif item_type in ("XControl", "PackedLibrary"):
                yield (None, False, True, name, False)  # opaque

    # Find all Target items first (depth 0 under them = top-level VIs)
    targets = root.findall(".//Item[@Type='Target']")
    if targets:
        for target in targets:
            yield from walk(target, 0)
    else:
        # Fallback: walk from root (handles .lvlib files directly)
        yield from walk(root, 0)


def enumerate_project_vis(lvproj_path: Path):
    """Return list of dicts describing each VI entry in the project."""
    entries = []
    seen_paths = set()

    for abs_path, is_top_level, is_opaque, name, is_external in _collect_items_from_xml(
        lvproj_path, lvproj_path.parent
    ):
        if is_opaque:
            entries.append({
                "vi_name": name,
                "abs_path": None,
                "relative_path": None,
                "is_top_level": False,
                "opaque": True,
                "external": False,
                "status": "opaque",
            })
            continue

        if is_external or abs_path is None:
            entries.append({
                "vi_name": name,
                "abs_path": None,
                "relative_path": None,
                "is_top_level": False,
                "opaque": False,
                "external": True,
                "status": "external",
            })
            continue

        # Deduplicate by absolute path
        path_key = str(abs_path).lower()
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)

        try:
            rel = abs_path.relative_to(lvproj_path.parent)
        except ValueError:
            rel = abs_path

        entries.append({
            "vi_name": abs_path.name,
            "abs_path": str(abs_path),
            "relative_path": str(rel),
            "is_top_level": is_top_level,
            "opaque": False,
            "external": False,
            "status": "pending",
        })

    return entries


# ---------------------------------------------------------------------------
# LabVIEW COM extraction helpers
# ---------------------------------------------------------------------------

def _safe_get(obj, attr, default=None):
    """Safely get a COM property, returning default on any error."""
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def _extract_controls(vi_com) -> tuple[list, list, bool]:
    """Return (inputs, outputs, has_error_terminals) from the VI's front panel."""
    inputs = []
    outputs = []
    has_error = False

    try:
        controls = vi_com.FrontPanel.Controls
        count = controls.Count
    except Exception:
        return inputs, outputs, has_error

    for i in range(count):
        try:
            ctrl = controls.Item(i)
            name = _safe_get(ctrl, "Name", "")
            cls = _safe_get(ctrl, "ClassNameString", "")
            is_indicator = _safe_get(ctrl, "IsIndicator", False)
            data_type = _safe_get(ctrl, "DataType", "")
            connector_pin = _safe_get(ctrl, "ConnectorPaneIndex", "")

            if "error" in name.lower():
                has_error = True

            terminal = {
                "name": name,
                "type": str(data_type),
                "description": "",
                "connector_pin": str(connector_pin),
            }
            if is_indicator:
                outputs.append(terminal)
            else:
                inputs.append(terminal)
        except Exception as e:
            log.debug("Control %d error: %s", i, e)

    return inputs, outputs, has_error


def _extract_diagram_info(vi_com, complexity_threshold: int) -> tuple[list, list, int]:
    """Return (diagram_structures, subvis_called, node_count) from block diagram."""
    structures = set()
    subvis = []
    node_count = 0

    try:
        bd = vi_com.BlockDiagram
        gobjects = bd.GObjects
        node_count = gobjects.Count
    except Exception:
        return list(structures), subvis, node_count

    for i in range(node_count):
        try:
            obj = gobjects.Item(i)
            cls = _safe_get(obj, "ClassNameString", "")
            if cls:
                structures.add(cls)

            if cls == "SubVI":
                try:
                    subvi_name = _safe_get(obj, "VIName") or _safe_get(obj.VI, "Name", "")
                    if subvi_name:
                        subvis.append(subvi_name)
                except Exception:
                    pass
        except Exception as e:
            log.debug("GObject %d error: %s", i, e)

    return sorted(structures), list(set(subvis)), node_count


def _export_image(vi_com, output_path: Path, export_bd: bool) -> bool:
    """Export block diagram (export_bd=True) or front panel (False) as PNG.

    Returns True on success.
    """
    try:
        # LabVIEW 2024 ExportImage(FilePath, Format, ExportBD)
        # Format: 0=PNG, 1=BMP, 2=JPEG
        vi_com.ExportImage(str(output_path), 0, export_bd)
        return output_path.exists()
    except Exception as e:
        log.debug("ExportImage(bd=%s) failed: %s — trying fallback", export_bd, e)

    # Fallback: try without the third argument (older API)
    if export_bd:
        try:
            vi_com.ExportImage(str(output_path), 0)
            return output_path.exists()
        except Exception as e2:
            log.debug("ExportImage fallback failed: %s", e2)

    return False


def extract_vi_metadata(vi_com, entry: dict, cfg: dict) -> dict:
    """Extract all metadata from a single VI COM object."""
    threshold = cfg["analysis"]["complexity_threshold_nodes"]

    inputs, outputs, has_error = _extract_controls(vi_com)
    structures, subvis, node_count = _extract_diagram_info(vi_com, threshold)

    # Build dependencies list: subvis that are external to the project
    # (This is a first-pass; full external detection happens in the caller)
    dependencies = []

    connector_pattern = ""
    try:
        connector_pattern = str(vi_com.ConnectorPane.Pattern)
    except Exception:
        pass

    return {
        "vi_name": _safe_get(vi_com, "Name", entry["vi_name"]),
        "relative_path": entry["relative_path"],
        "description": _safe_get(vi_com, "Description", ""),
        "connector_pane_pattern": connector_pattern,
        "inputs": inputs,
        "outputs": outputs,
        "subvis_called": subvis,
        "dependencies": dependencies,
        "has_error_terminals": has_error,
        "diagram_structures": structures,
        "node_count": node_count,
        "is_reentrant": bool(_safe_get(vi_com, "Reentrant", False)),
        "is_top_level": entry["is_top_level"],
    }


# ---------------------------------------------------------------------------
# Main extraction loop
# ---------------------------------------------------------------------------

def run_extraction(lvproj_path: Path, cfg: dict):
    import win32com.client  # Only available on Windows
    import pythoncom

    pythoncom.CoInitialize()
    meta_dir = get_subdir(cfg, "metadata")
    img_dir = get_subdir(cfg, "images")
    export_images = cfg["labview"]["export_images"]

    log.info("Connecting to running LabVIEW instance via COM...")
    try:
        app = win32com.client.Dispatch("LabVIEW.Application")
        log.info("Connected. LabVIEW version: %s", _safe_get(app, "Version", "unknown"))
    except Exception as e:
        log.error("Failed to connect to LabVIEW: %s", e)
        log.error("Make sure LabVIEW 2024 is running and VI Server is enabled.")
        sys.exit(1)

    log.info("Parsing project: %s", lvproj_path)
    entries = enumerate_project_vis(lvproj_path)
    log.info("Found %d VI entries in project tree", len(entries))

    manifest = []
    dependency_graph = {}

    for idx, entry in enumerate(entries, 1):
        vi_name = entry["vi_name"]
        log.info("[%d/%d] %s", idx, len(entries), vi_name)

        manifest_entry = {**entry}

        # Skip opaque/external without COM
        if entry["status"] in ("opaque", "external"):
            manifest_entry["has_metadata"] = False
            manifest_entry["has_bd_image"] = False
            manifest_entry["has_fp_image"] = False
            manifest.append(manifest_entry)
            continue

        safe_name = sanitize_vi_name(vi_name)
        metadata_path = meta_dir / f"{safe_name}.json"
        bd_image_path = img_dir / f"{safe_name}_bd.png"
        fp_image_path = img_dir / f"{safe_name}_fp.png"

        try:
            abs_path = entry["abs_path"]
            vi_com = app.GetVIReference(abs_path)

            # Extract metadata
            metadata = extract_vi_metadata(vi_com, entry, cfg)

            # Write metadata JSON
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)

            # Export images
            has_bd = False
            has_fp = False
            if export_images:
                has_bd = _export_image(vi_com, bd_image_path, export_bd=True)
                has_fp = _export_image(vi_com, fp_image_path, export_bd=False)
                if not has_bd:
                    log.warning("Block diagram image export failed for %s", vi_name)
                if not has_fp:
                    log.debug("Front panel image export failed for %s (non-critical)", vi_name)

            # Build dependency graph entry
            dependency_graph[vi_name] = metadata["subvis_called"]

            manifest_entry["status"] = "ok"
            manifest_entry["has_metadata"] = True
            manifest_entry["has_bd_image"] = has_bd
            manifest_entry["has_fp_image"] = has_fp
            manifest_entry["node_count"] = metadata["node_count"]

        except Exception as e:
            log.warning("FAILED to extract %s: %s", vi_name, e)
            manifest_entry["status"] = "error"
            manifest_entry["error"] = str(e)
            manifest_entry["has_metadata"] = False
            manifest_entry["has_bd_image"] = False
            manifest_entry["has_fp_image"] = False

        manifest.append(manifest_entry)

    # Write aggregate outputs
    output_dir = get_subdir(cfg, "")  # output root
    dep_graph_path = output_dir / "dependency_graph.json"
    manifest_path = output_dir / "project_manifest.json"

    with open(dep_graph_path, "w", encoding="utf-8") as f:
        json.dump(dependency_graph, f, indent=2)

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Summary
    ok = sum(1 for e in manifest if e["status"] == "ok")
    err = sum(1 for e in manifest if e["status"] == "error")
    opaque = sum(1 for e in manifest if e["status"] == "opaque")
    ext = sum(1 for e in manifest if e["status"] == "external")

    log.info("Extraction complete: %d ok, %d errors, %d opaque, %d external",
             ok, err, opaque, ext)
    log.info("Manifest: %s", manifest_path)
    log.info("Dependency graph: %s", dep_graph_path)


def main():
    parser = argparse.ArgumentParser(
        description="Extract metadata and images from a LabVIEW project."
    )
    parser.add_argument("project_file", help="Path to the .lvproj file")
    parser.add_argument("--config", default=None, help="Path to config.yaml (optional)")
    args = parser.parse_args()

    lvproj_path = Path(args.project_file).resolve()
    if not lvproj_path.exists():
        log.error("Project file not found: %s", lvproj_path)
        sys.exit(1)
    if not lvproj_path.suffix.lower() == ".lvproj":
        log.error("Expected a .lvproj file, got: %s", lvproj_path)
        sys.exit(1)

    cfg = load_config(args.config)
    run_extraction(lvproj_path, cfg)


if __name__ == "__main__":
    main()
