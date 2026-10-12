# Campaign examples

These manifests demonstrate bounded, reviewed Slurm campaign operations. Use a
configured Raven installation and a site that supports the requested operation.

The examples preserve native campaign contracts. Managed Raven supports admitted
file inspection, while campaign Apply and external-driver paths remain deferred.
Heavy admission does not enable those handoffs. Read the
[execution contracts](../../raven/execution-contracts.md) before attempting a
campaign.

Copy this directory to a shared project directory before you edit or submit an
example. Open the matching operation with `:ops search`, `:ops checkpoint`,
`:ops packing`, `:ops dask`, or `:ops heterogeneous`. Set **Manifest JSON** to the
copied file. Inspection does not submit work.

- `search.json` evaluates five integer values with `trial.sbatch`. After each
  job finishes, copy its printed result into the manifest's `observations` list.
  Raven persists the launch journal separately. Use a new campaign ID for a
  deliberately separate experiment.
- `checkpoint.json` validates a small demonstration checkpoint and restart
  reader. Its source cluster, job ID, and start time are examples. Replace them
  with the exact terminated source attempt before Apply. Replace the reader
  with an actual application restart and update both SHA-256 values after edits.
- `packing.json` runs two independent Python commands in exclusive Slurm steps.
- `dask.json` creates an optional adaptive pool only after a reviewed Start.
  Install and configure `dask-jobqueue` first. A running pool survives Raven exit;
  use the reviewed Stop action when you finish.
- `heterogeneous.json` requests two components and prints their hostnames. The
  site must support heterogeneous jobs.

Add site account or partition settings where needed. See the
[campaign guide](../../guides/operations-campaigns.md) for contracts, bounds,
receipts, and failure handling.
