"""Download the official pinned CPU embedding model into the D-drive project cache."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / ".data" / "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
sys.path.insert(0, str(ROOT / "backend"))

from huggingface_hub import snapshot_download

MODEL = "Qwen/Qwen3-Embedding-0.6B"
REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"

if __name__ == "__main__":
    path = snapshot_download(
        MODEL,
        revision=REVISION,
        allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "1_Pooling/*"],
        max_workers=2,
    )
    print(f"Official Qwen revision: {REVISION}")
    print(f"Model cache: {path}")
