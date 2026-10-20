"""Compatibility module for :mod:`raven.recordings.worker`."""

if __name__ == "__main__":
    from .recordings.worker import main

    raise SystemExit(main())
else:
    import sys
    from .recordings import worker as _implementation

    sys.modules[__name__] = _implementation
