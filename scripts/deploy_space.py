"""Deploy Receipts to a Hugging Face Docker Space.

    pip install huggingface_hub
    hf auth login                      # once, with a write token
    python scripts/deploy_space.py <username>/<space-name>

Creates the Space (Docker SDK) if needed, uploads the code with deploy/space_README.md as the
Space README, and stores GROQ_API_KEY from .env as a Space secret (never as a file).
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values
from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parent.parent
IGNORE = [".env", ".venv/*", ".git/*", ".unlazy/*", ".claude/*", "**/__pycache__/*", "*.db", "*.db-*",
          "runs/*", "memory/*", "policy_overrides.json", "README.md", "deploy/*", ".unlazy-hook-state.json"]


def main(repo_id: str) -> None:
    key = dotenv_values(ROOT / ".env").get("GROQ_API_KEY", "")
    if not key:
        sys.exit("GROQ_API_KEY missing from .env")
    api = HfApi()
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)
    api.add_space_secret(repo_id, "GROQ_API_KEY", key)
    api.upload_folder(repo_id=repo_id, repo_type="space", folder_path=str(ROOT), ignore_patterns=IGNORE,
                      commit_message="Deploy Receipts")
    api.upload_file(repo_id=repo_id, repo_type="space", path_or_fileobj=str(ROOT / "deploy" / "space_README.md"),
                    path_in_repo="README.md", commit_message="Space README")
    print(f"Deployed: https://huggingface.co/spaces/{repo_id}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or "/" not in sys.argv[1]:
        sys.exit("usage: python scripts/deploy_space.py <username>/<space-name>")
    main(sys.argv[1])
