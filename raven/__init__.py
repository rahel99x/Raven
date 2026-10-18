"""Raven's application API; dashboard dependencies are imported only on demand."""
__version__ = "7.0.0"
__all__ = ["Kind", "configure", "measure", "metric", "phase", "progress", "watch"]


def __getattr__(name):
    if name in __all__:
        from . import client
        value = getattr(client, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
