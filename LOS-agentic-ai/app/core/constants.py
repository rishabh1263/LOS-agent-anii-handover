from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "app" / "config"
DEFAULT_UPLOAD_ROOT = PROJECT_ROOT / "runtime" / "uploads"
