"""Export the OpenAPI spec for frontend client generation.

Usage: uv run python scripts/export_openapi.py > frontend/openapi.json
The generated TS types are checked in; CI regenerates and fails on drift.
"""

import json
import os
import sys

from cryptography.fernet import Fernet

# create_app requires an encryption key at import-of-app time (issue #93);
# schema export never touches encrypted data, so a throwaway key is fine.
os.environ.setdefault("PURSER_ENCRYPTION_KEY", Fernet.generate_key().decode())

from purser.api.app import create_app


def main() -> None:
    spec = create_app().openapi()
    json.dump(spec, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
