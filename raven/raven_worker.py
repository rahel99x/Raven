"""Compatibility module for :mod:`raven.heavy.worker`."""

if __name__ == "__main__":
    if not __package__:
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from raven.heavy.worker import main

    raise SystemExit(main())
else:
    import sys
    from .heavy import worker as _implementation

    sys.modules[__name__] = _implementation
