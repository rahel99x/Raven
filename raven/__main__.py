"""Entry point: ``raven`` once installed, or ``python3 -m raven`` / ``python3 raven`` (the package directory) from a clone."""
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from raven.raven_launch import light_main as main
else:
    from .raven_launch import light_main as main

sys.exit(main())
