"""CLI result ownership independent of parsing and launch implementations."""


class ApplicationExit(int):
    """An executed workload owns stdout, stderr and this process exit status.

    Ordinary integer failures still belong to Raven's machine-readable error
    boundary. This marker is published only after a local workload executed.
    """
