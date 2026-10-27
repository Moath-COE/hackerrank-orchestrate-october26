"""Filesystem locations, resolved relative to the repository root so runs work from any cwd."""

from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_DIR.parent
DATASET_DIR = REPO_ROOT / "dataset"
OUTPUT_CSV = REPO_ROOT / "output.csv"
SAMPLE_OUTPUT_CSV = REPO_ROOT / "output_sample.csv"
CACHE_DIR = CODE_DIR / ".cache"
DB_PATH = CACHE_DIR / "audit.db"
