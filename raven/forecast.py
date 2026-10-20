"""Public queue forecast API and scenario entry point."""
from . import forecast_model as _model

# Preserve private helpers used by released extensions as well as public API.
globals().update({name: value for name, value in vars(_model).items() if not name.startswith("__")})

def scenarios(document, *, cancel=None):
    """Compare explicit request alternatives without changing scheduler state."""
    from .forecast_scenarios import analyze
    return analyze(document, cancel=cancel)
