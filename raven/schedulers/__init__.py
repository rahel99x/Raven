"""Explicit native scheduler registry; importing it performs no discovery I/O."""
from .core import Scheduler, normalize_scope, scope_digest, validate_identity

NAMES = ("slurm", "kubernetes", "lsf", "openpbs", "pbspro", "htcondor", "flux")


def create(name, backend, scope=None, **limits):
    """Construct only installed, audited adapters; no project module imports."""
    if name == "slurm":
        from .slurm import SlurmScheduler
        adapter = SlurmScheduler
    elif name == "kubernetes":
        from .kubernetes import KubernetesScheduler
        adapter = KubernetesScheduler
    elif name == "lsf":
        from .lsf import LSFScheduler
        adapter = LSFScheduler
    elif name in {"openpbs", "pbspro"}:
        from .pbs import OpenPBSScheduler, PBSProfessionalScheduler
        adapter = OpenPBSScheduler if name == "openpbs" else PBSProfessionalScheduler
    elif name == "htcondor":
        from .htcondor import HTCondorScheduler
        adapter = HTCondorScheduler
    elif name == "flux":
        from .flux import FluxScheduler
        adapter = FluxScheduler
    else:
        raise ValueError("Unknown scheduler; choose one of " + ", ".join(NAMES))
    return adapter(backend, scope, **limits)


__all__ = ["NAMES", "Scheduler", "create", "normalize_scope", "scope_digest", "validate_identity"]
