"""Read-only ownership checks for diagnostic process receipts."""
import os
from pathlib import Path
from . import science_contracts


def _process_matches(record):
    try:
        pid = science_contracts.integer(record["pid"], "driver pid", 1, 2147483647)
        if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != record["boot"]:
            return False
        process = Path(f"/proc/{pid}")
        if process.stat().st_uid != os.geteuid():
            return False
        data = (process/"stat").read_text()
        fields = data[data.rfind(")")+2:].split()
        return fields[19] == record["start"] and fields[0] != "Z"
    except (OSError, ValueError, KeyError, IndexError):
        return False
