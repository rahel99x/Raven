#!/bin/bash
set -euo pipefail
# Only disposable containers created by qualify_slurm.py use these identities.
umask 077
dd if=/dev/urandom of=/etc/munge/munge.key bs=1024 count=1 status=none
chown munge:munge /etc/munge/munge.key
runuser -u munge -- munged
mariadbd --user=mysql --skip-networking --socket=/run/mysqld/mysqld.sock &
for attempt in {1..100}; do
    if mariadb-admin --socket=/run/mysqld/mysqld.sock ping --silent; then break; fi
    sleep .2
done
mariadb --socket=/run/mysqld/mysqld.sock <<'SQL'
CREATE DATABASE IF NOT EXISTS slurm_acct_db;
CREATE USER IF NOT EXISTS 'slurm'@'localhost' IDENTIFIED BY 'disposable-ci-only';
GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';
SQL
cat > /etc/slurm/slurmdbd.conf <<'CONF'
AuthType=auth/munge
DbdHost=localhost
SlurmUser=slurm
StorageType=accounting_storage/mysql
StorageHost=localhost
StorageUser=slurm
StoragePass=disposable-ci-only
StorageLoc=slurm_acct_db
LogFile=/var/log/slurm/slurmdbd.log
PidFile=/tmp/slurmdbd.pid
CONF
chown slurm:slurm /etc/slurm/slurmdbd.conf
cat > /etc/slurm/slurm.conf <<'CONF'
ClusterName=raven-ci
SlurmctldHost=raven-ci
SlurmUser=slurm
AuthType=auth/munge
AuthInfo=cred_expire=10
StateSaveLocation=/var/spool/slurmctld
SlurmdSpoolDir=/var/spool/slurmd
SlurmctldPidFile=/tmp/slurmctld.pid
SlurmdPidFile=/tmp/slurmd.pid
SlurmctldLogFile=/var/log/slurm/slurmctld.log
SlurmdLogFile=/var/log/slurm/slurmd.log
ProctrackType=proctrack/linuxproc
TaskPlugin=task/none
JobAcctGatherType=jobacct_gather/linux
JobAcctGatherFrequency=task=1
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageHost=localhost
AccountingStorageEnforce=associations
SelectType=select/cons_tres
SelectTypeParameters=CR_CPU_Memory
SchedulerType=sched/backfill
SchedulerParameters=requeue_delay=1
ReturnToService=2
SlurmctldParameters=enable_configless
MinJobAge=30
NodeName=raven-ci CPUs=2 RealMemory=1024 State=UNKNOWN
PartitionName=local Nodes=raven-ci Default=YES MaxTime=00:10:00 State=UP
CONF
chmod 644 /etc/slurm/slurm.conf
case "$(slurmd --version)" in
    'slurm 23.'*|'slurm 24.'*)
        # Older Slurm does not recognize disabled. The container owns its cgroup
        # namespace; task/none and linuxproc do not activate cgroup controllers.
        printf '%s\n' 'CgroupPlugin=cgroup/v1' > /etc/slurm/cgroup.conf ;;
    *) printf '%s\n' 'CgroupPlugin=disabled' > /etc/slurm/cgroup.conf ;;
esac
chmod 644 /etc/slurm/cgroup.conf
slurmdbd
for attempt in {1..100}; do
    if sacctmgr -n show cluster >/dev/null 2>&1; then break; fi
    sleep .2
done
sacctmgr -i add cluster raven-ci
sacctmgr -i add account labx Description='Disposable lab qualification' Organization='Raven CI'
sacctmgr -i add user raven Account=labx
slurmctld
slurmd
touch /tmp/raven-services-started
exec sleep infinity
