"""Entry point: ``tower`` once installed, or ``python3 -m tower`` / ``python3 tower`` (the package directory) from a clone."""
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tower.raven_launch import legacy_main as main
else:
    from .raven_launch import legacy_main as main

sys.exit(main())
