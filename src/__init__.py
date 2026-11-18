"""AI-Team runtime package."""

import sys

if sys.version_info < (3, 12):
    raise RuntimeError(
        "AI-Team requires Python 3.12 or newer. Recreate .venv with Python 3.12 "
        "and install the project from pyproject.toml before starting a service."
    )
