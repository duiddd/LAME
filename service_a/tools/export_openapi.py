"""Writes the OpenAPI contract the frontend generates its types from: python -m tools.export_openapi"""
import json
from pathlib import Path

from app.main import create_app

OUT = Path(__file__).resolve().parent.parent / "openapi.json"

if __name__ == "__main__":
    OUT.write_text(json.dumps(create_app().openapi(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
