#!/bin/bash
#SBATCH --cpus-per-task=1
#SBATCH --mem=128M
#SBATCH --time=00:05:00
# Replace this demonstration reader with the application's explicit restart.
python3 - <<'PY'
import os
from pathlib import Path
print(Path(os.environ["RAVEN_CHECKPOINT"]).read_text())
PY
