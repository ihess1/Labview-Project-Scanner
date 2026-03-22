"""Loads config.yaml and provides helpers used by all pipeline scripts."""

from pathlib import Path
import yaml

# Repo root is one level above this file
_REPO_ROOT = Path(__file__).parent.parent
_CONFIG_PATH = _REPO_ROOT / "config.yaml"


def load_config(config_path: str | Path | None = None) -> dict:
    """Load and return the configuration dictionary.

    Args:
        config_path: Optional override path to config.yaml. Defaults to
                     the config.yaml in the repository root.
    """
    path = Path(config_path) if config_path else _CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg


def get_output_dir(cfg: dict) -> Path:
    """Return the configured output root directory, creating it if needed."""
    output_dir = Path(cfg["paths"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_subdir(cfg: dict, subdir: str) -> Path:
    """Return a named subdirectory under output_dir, creating it if needed."""
    d = get_output_dir(cfg) / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_vi_name(vi_name: str) -> str:
    """Convert a VI name to a safe filename stem (no extension)."""
    # Strip .vi extension if present
    name = vi_name
    if name.lower().endswith(".vi"):
        name = name[:-3]
    # Replace path separators and spaces
    for ch in r"/\: ":
        name = name.replace(ch, "_")
    return name
