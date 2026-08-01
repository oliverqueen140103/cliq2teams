"""Allows `python -m c2t ...` as well as `python -m c2t.cli ...`."""
from .cli import main

raise SystemExit(main())
