# Enable Slurm accounting on a Fedora desktop

[Desktop guide](DESKTOP.md) · [Runbook](runbook.md)

Use this procedure to enable persistent job history on a personal, single-machine Slurm installation.
Raven reads this history through `sacct`.
The procedure adds MariaDB and `slurmdbd` to an existing working scheduler.

This procedure targets Fedora packages with Slurm 24.05 and MariaDB 10.11.
The controller, worker, database, and accounting daemon run on the same computer.
The existing Slurm configuration must use `SlurmUser=slurm`.
The controller must communicate over loopback before you apply the accounting daemon address filter.
For this single-machine setup, the existing controller setting must use an explicit loopback address,
for example `SlurmctldHost=HOSTNAME_SHORT(127.0.0.1)`.
Replace `HOSTNAME_SHORT` with the actual short hostname.

## 1. Check the existing scheduler

Run these commands as your normal login user:

```bash
hostname -s
id -un
sinfo --version
scontrol show config | rg '^\s*(ClusterName|SlurmUser)'
squeue
```

Record the short hostname and the existing `ClusterName`.
The cluster name is often `cluster`; use the value from your configuration.
Do not change the cluster name to add accounting.

Set these variables in the same terminal:

```bash
ACCOUNTING_CLUSTER=CLUSTER_NAME
ACCOUNTING_USER=$(id -un)
```

Replace `CLUSTER_NAME` with the existing cluster name.
Wait for all running and pending jobs to finish before the daemon restart in step 8.

Back up the configuration:

```bash
sudo cp -a /etc/slurm "/etc/slurm.backup-accounting-$(date +%Y%m%d-%H%M%S)"
```

## 2. Install matching packages

```bash
sudo dnf install mariadb-server slurm-slurmdbd
rpm -q slurm slurm-slurmctld slurm-slurmd slurm-slurmdbd mariadb-server
systemctl cat slurmdbd
```

**Expected result:** All Slurm packages have the same version and release.
The Fedora `slurmdbd` service uses `User=slurm` and `Group=slurm`.
Resolve version or service-account differences before continuing.

## 3. Configure the local database

This step assumes a new MariaDB installation.
If MariaDB already serves other applications, review the proposed global settings before applying them.

Open the configuration:

```bash
sudo nvim /etc/my.cnf.d/90-slurm-accounting.cnf
```

Add:

```ini
[mariadb]
bind-address=127.0.0.1
innodb_buffer_pool_size=4G
innodb_log_file_size=1G
innodb_lock_wait_timeout=900
max_allowed_packet=16M
innodb_snapshot_isolation=OFF
```

The buffer pool can consume approximately 4 GiB of RAM.
Reserve that memory outside Slurm job allocations.
Use these values with MariaDB 10.11; review version-specific options when using another version.

Start the database:

```bash
sudo restorecon -RF /etc/my.cnf.d
sudo systemctl enable --now mariadb
```

If MariaDB was already running when you changed its configuration, restart it:

```bash
sudo systemctl restart mariadb
```

Check for an existing accounting database or database user:

```bash
sudo mariadb -e "SELECT User,Host,plugin FROM mysql.user WHERE User='slurm'; SHOW DATABASES LIKE 'slurm_acct_db';"
```

If either already exists, inspect the existing setup before continuing.
Do not reset an existing database user or delete an existing database.

For a fresh setup, open the database console:

```bash
sudo mariadb
```

Run:

```sql
CREATE DATABASE slurm_acct_db;
CREATE USER 'slurm'@'localhost' IDENTIFIED VIA unix_socket;
GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';
EXIT;
```

The `unix_socket` authentication plugin checks the operating-system identity of a local socket connection.
The service account `slurm` can authenticate as the database user `slurm`.
The login user does not need a database password.
See the [MariaDB unix_socket authentication documentation](https://mariadb.com/docs/server/reference/plugins/authentication-plugins/authentication-plugin-unix-socket).

Verify the connection as the service account:

```bash
sudo -u slurm mariadb --protocol=SOCKET -u slurm --database=slurm_acct_db -e 'SELECT USER(), CURRENT_USER();'
```

**Expected result:** The command succeeds and reports `slurm@localhost` as the authenticated database user.

## 4. Configure the accounting daemon

Open:

```bash
sudo nvim /etc/slurm/slurmdbd.conf
```

Add:

```ini
SlurmUser=slurm
AuthType=auth/munge
DbdHost=HOSTNAME_SHORT
DbdAddr=127.0.0.1
DbdPort=6819

StorageType=accounting_storage/mysql
StorageHost=localhost
StorageLoc=slurm_acct_db
StorageUser=slurm

PidFile=/run/slurmdbd/slurmdbd.pid
DebugLevel=info
```

Replace `HOSTNAME_SHORT` with the output of `hostname -s`.
Keep `StorageHost=localhost` so the database client uses the local Unix socket.
Omit `StoragePass` for the socket-authenticated database user.

Apply ownership and labels:

```bash
sudo chown slurm:slurm /etc/slurm/slurmdbd.conf
sudo chmod 0600 /etc/slurm/slurmdbd.conf
sudo restorecon -RF /etc/slurm
```

Create the service override directory and open the override:

```bash
sudo install -d -m 0755 /etc/systemd/system/slurmdbd.service.d
sudo nvim /etc/systemd/system/slurmdbd.service.d/10-local-accounting.conf
```

Add:

```ini
[Unit]
Requires=mariadb.service munge.service
After=mariadb.service munge.service

[Service]
IPAddressDeny=any
IPAddressAllow=localhost
```

The address filter restricts this service's IP traffic to loopback.
It still permits the local Unix socket used for the database.
`DbdAddr` identifies the daemon address; it does not limit the listening socket to that address.
The service filter supplies that local restriction.

## 5. Connect the scheduler to accounting

Open:

```bash
sudo nvim /etc/slurm/slurm.conf
```

Add these settings once.
If any setting already exists, replace its existing value instead of adding a duplicate.

```ini
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageHost=127.0.0.1
AccountingStoragePort=6819
AccountingStorageTRES=gres/gpu
JobAcctGatherType=jobacct_gather/cgroup
JobAcctGatherFrequency=task=10
```

Leave `AccountingStorageEnforce` unset for this personal setup.
The procedure records usage without adding association or allocation limits.
Keep your existing CPU, memory, GPU, and partition configuration.

Create a controller service override:

```bash
sudo install -d -m 0755 /etc/systemd/system/slurmctld.service.d
sudo nvim /etc/systemd/system/slurmctld.service.d/10-accounting-order.conf
```

Add:

```ini
[Unit]
Wants=slurmdbd.service
After=slurmdbd.service
```

Apply labels and reload the service definitions:

```bash
sudo restorecon -RF /etc/slurm /etc/systemd/system/slurmdbd.service.d /etc/systemd/system/slurmctld.service.d
sudo systemctl daemon-reload
```

The file on disk now allows `sacctmgr` to find the accounting daemon.
The running controller does not use the new settings until step 8.

## 6. Start the accounting daemon

```bash
sudo systemctl enable --now munge
sudo systemctl enable --now slurmdbd
systemctl --no-pager --full status mariadb munge slurmdbd
sudo journalctl -b -u slurmdbd --no-pager -n 60
```

**Expected result:** All three services are active.
The accounting daemon creates its tables without database or authentication errors.

## 7. Register the cluster and login user

Run these commands only after `slurmdbd` is active:

```bash
sudo sacctmgr -i add cluster "$ACCOUNTING_CLUSTER"
sudo sacctmgr -i add account personal Cluster="$ACCOUNTING_CLUSTER" Description="Personal jobs" Organization="Personal"
sudo sacctmgr -i add user "$ACCOUNTING_USER" Cluster="$ACCOUNTING_CLUSTER" Account=personal DefaultAccount=personal
sudo sacctmgr show cluster
sudo sacctmgr show associations where Cluster="$ACCOUNTING_CLUSTER" format=Cluster,Account,User
```

**Expected result:** The existing cluster and the login user's `personal` association appear.
Do not add a second cluster name for the same controller.
If an entity already exists, inspect its association instead of deleting and recreating it.

## 8. Activate accounting in the scheduler

Check the whole queue again:

```bash
squeue
```

If jobs remain, wait for them to finish.
Then restart the controller and worker:

```bash
sudo systemctl restart slurmctld
sudo systemctl restart slurmd
scontrol ping
sinfo
scontrol show config | rg 'AccountingStorage|JobAcctGather'
```

**Expected result:** The controller is up and the node is available.
The configuration output reports `accounting_storage/slurmdbd` and `jobacct_gather/cgroup`.

## 9. Verify a new job and Raven

Submit a small CPU job as your normal login user:

```bash
ACCOUNTING_JOB_ID=$(sbatch --parsable --account=personal --job-name=accounting-smoke --ntasks=1 --cpus-per-task=1 --mem=256M --time=00:01:00 --wrap='hostname; sleep 12')
squeue -j "$ACCOUNTING_JOB_ID"
```

This uses the existing default partition.
After the job leaves the queue, inspect its accounting record:

```bash
sacct -j "$ACCOUNTING_JOB_ID" --format=JobID,JobName,State,ExitCode,Elapsed,AllocCPUS,ReqMem,MaxRSS,TotalCPU
```

**Expected result:** The job has state `COMPLETED` and exit code `0:0`.
Its batch-step record supplies resource measurements when available.
A short job can have sparse usage samples.

From the Raven checkout, run:

```bash
./scripts/tower --config docs/config.example.json --profile desktop --once --tab sources
./scripts/tower --config docs/config.example.json --profile desktop
```

**Expected result:** The `finished` source succeeds and History contains the new job.
If a Raven session had accounting sources disabled, restart Raven or enter `:source finished on` and `:source fin_details on` in the command palette.

## 10. Limits and troubleshooting

Accounting starts with the newly configured scheduler.
This procedure does not reconstruct previously completed jobs.
Old output files remain application evidence.

`AccountingStorageTRES=gres/gpu` records allocated GPU resources.
It does not by itself provide GPU utilization or GPU-memory measurements.
Those measurements need supported GPU accounting and detection plugins.
Raven's desktop profile continues to disable live GPU sampling.

If a service fails, inspect:

```bash
sudo journalctl -b -u mariadb -u munge -u slurmdbd -u slurmctld -u slurmd --no-pager -n 100
```

| Symptom | Check |
| --- | --- |
| Database authentication fails | Confirm `slurmdbd.service` runs as `slurm`, `StorageHost=localhost`, and the database user's plugin is `unix_socket`. Repeat the service-account connection test. |
| Database settings are rejected | Confirm MariaDB 10.11 and inspect the exact unknown option in the MariaDB log. |
| Accounting daemon cannot register the cluster | Confirm the existing `ClusterName` matches the database cluster record. |
| `sacct` still reports disabled accounting | Check the active `AccountingStorageType`, then confirm the controller restarted after the configuration change. |
| Raven History is empty | Query the new job directly with `sacct`. Confirm the job is in Raven's history window and belongs to the selected user. |
| Service address filter is unavailable | Inspect systemd's service logs and cgroup support before relying on loopback-only service traffic. |

Retain database backups when job history matters.
Review database growth and retention as usage increases.
See [Slurm accounting administration](https://slurm.schedmd.com/accounting.html) for storage, cluster registration, and retention behavior.
