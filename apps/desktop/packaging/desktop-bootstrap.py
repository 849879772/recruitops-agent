"""Immutable packaged bootstrap: never import the checkout or a developer venv."""
import sys
from pathlib import Path

resources = Path(__file__).resolve().parent
application = resources / "desktop-runtime" / "app"
if not (application / "packages" / "desktop_runtime" / "__main__.py").is_file():
    raise SystemExit("packaged_application_missing")
sys.path.insert(0, str(application))
from packages.desktop_runtime.__main__ import main

raise SystemExit(main())
