# Experimental shared host collector

The shared collector serves authorized projections of one host's read-only
scheduler observations. It is an optional administrator-reviewed service. Ordinary
Raven installation and the per-user session service do not enable it.

The Unix socket authenticates a UID through Linux `SO_PEERCRED`. A separate policy
grants that UID its own scheduler login, named Raven projects, explicitly named lab accounts, or site
visibility. Socket group membership permits connecting; it does not grant job
visibility. The service never accepts scheduler commands, mutations, file paths,
log reads or configuration writes from clients.

## Review a visibility policy

Create a protected JSON policy, owned by the service account or root and without
group/world write permission. Every directory ancestor must also be protected.
For example, substitute the actual UID, login and socket group:

```json
{
  "schema": "raven.shared-host-policy/v1",
  "id": "research-login-observations",
  "socket_gid": 4000,
  "interval_seconds": 60,
  "max_records": 10000,
  "principals": {
    "1100": {"user": "student", "accounts": [], "site": false},
    "1101": {"user": "lab-manager", "accounts": ["LabX"], "site": false}
  }
}
```

The UID must resolve to the declared login through the host's account database
when the policy loads. Administrators must revoke or update grants before UID
reuse or account reassignment. The request path does not perform synchronous
directory-service lookups; directory-service lifecycle handling is part of site
qualification.
Empty account/project lists authorize only the user's own jobs. `accounts: ["LabX"]`
explicitly grants the lab's job projection; the service does not infer that
permission from a job name or client-supplied account. `site: true` authorizes all
rows visible to the service account and should be granted only after site review.
Scheduler `PrivateData` and the service account's own privileges still constrain
what can be observed. The collector does not bypass them.

### Share a project without sharing its billing account

Several independent Raven projects can charge the same lab account. Account
membership therefore cannot establish project membership. To share only one
project, an administrator adds `project_memberships` to the protected policy and
names that project in the principal's optional `projects` list:

```json
{
  "project_memberships": {
    "ProjectA": [{"cluster": "research", "job_id": "123_4", "submit_time": 1792279000,
                  "start_time": 1792279060, "restart_count": 0}],
    "ProjectB": [{"cluster": "research", "job_id": "124", "submit_time": 1792279000,
                  "start_time": 1792279061, "restart_count": 0}]
  },
  "principals": {
    "1100": {"user": "student", "accounts": [], "projects": ["ProjectA"], "site": false}
  }
}
```

This is a policy fragment: keep the required schema, service ID, group and bounds
from the complete example. Use the actual `project_id` from the reviewed Raven
project inventory; an inventory without a project ID must receive one before
the administrator binds it. The two examples may both charge `LabX`, but this
principal sees only ProjectA's listed attempt, plus their own jobs. Grants are a
union: adding `accounts: ["LabX"]` deliberately shares both projects' lab jobs.

The administrator must establish membership through a trusted submission or
approval record and verify the native scheduler attempt. The identity requires
the exact cluster, job/task/component ID, positive submission/start Unix seconds,
and known native restart count. `squeue --json` exposes `submit_time`, `start_time`
and `restart_cnt`; parent records from `sacct --json -X` expose `time.submission`,
`time.start` and `restart_cnt`. Qualified numeric sentinel objects must be set and
finite. Array ranges and step IDs are not parent attempts. A research run's own
attempt number is not Slurm's restart count.

User-controlled project files, job names, comments, directory names and account
labels are not authority. Raven does not discover or trust them automatically.
If any native field is unknown, project-only visibility abstains; missing restart
never means zero. Queue project bindings require a running, suspended or completing
execution. Pending/configuring start times may be estimates and never authorize
project-only visibility; those jobs remain available through own-user/account/site
grants. Requeues, reused job IDs and different
clusters require a new exact approved membership. The current contract does not
accept client-supplied submission tokens.

Replace the protected policy atomically to add or revoke membership. Membership
changes invalidate the same caches and pending responses as principal changes.
The policy accepts at most 256 projects, 64 project grants per principal and
1,024 total exact memberships within 256 KiB. Archive old memberships under the
site's retention policy. This explicit bounded map is a local deployment
contract; automating a site's authoritative membership source requires an
administrator integration and separate qualification.

## Run and connect

An administrator chooses the service account, creates the approved Unix group,
adds permitted clients, and selects a local filesystem. Those are deployment
actions, not operations performed by Raven. Start in the foreground first:

```bash
python -m raven.shared_host_service serve /run/raven-observations \
  --policy /etc/raven/shared-observations.json \
  --config /etc/raven/config.json --site local-slurm
```

The service directory must be owned by the service account, mode `0750`, with the
approved group. A newly created directory receives that ownership/group; existing
unsafe directories are refused. The socket is `0660`, private retained files are
`0600`, and peers must still pass policy authorization. The client independently
chooses the service UID; this example assumes an administrator-approved UID 500:

```bash
python -m raven.shared_host_service query /run/raven-observations --server-uid 500
python -m raven.shared_host_service watch /run/raven-observations --server-uid 500
python -m raven.shared_host_service tui /run/raven-observations --server-uid 500
```

The read-only terminal shows the effective login/account/project/site grants and
labels rows with the projects that authorize them. It uses the existing session
client's background reader; keyboard input does not wait for IPC. Query/watch
responses expose the same authorized scope and source freshness. Reconnect after
an administrator changes the policy; the client never selects its own grants.

The effective polling interval is the stricter of this policy and the normal
managed Raven site configuration. Source collection uses the existing governed
backend, its admission ledger, bounded output and shared-query cache. The fixed
native reads are `squeue --json` and bounded, recent `sacct --json --allusers`.
Only recorded, supported structured parser versions are accepted; ambiguous or
missing ownership is never replaced by a broad text fallback.

## Visibility, freshness and bounds

Authorization filters rows before serialization, then selects explicit job
identity/state/resource/time columns. Commands, work directories, raw scheduler
output, configuration, logs and another user's rows are never placed in a peer's
response. Project/lab/site grants intentionally expand the authorized row set. No global
row counts or global event sequence are sent to an ordinary user's view.

View cache identity includes the authenticated UID, exact entitlement and policy
generation. Atomic policy replacement is detected before the next selector turn;
newly observed revocation drops old connections and their pending bytes and
clears authorized caches. Bytes delivered before revocation cannot be recalled.
Malformed policy fails closed until corrected. Changing the service ID or socket
group requires a reviewed stop/restart. Reconnect explicitly after a grant change.

Queue and accounting observations have separate timestamps and source-error
indicators. A failed read preserves the last authorized observation with a stale
label; one source recovering does not mark a different failed source fresh.
Detailed scheduler errors remain in the service account's diagnostics because
raw error text can contain another user's job attributes.

The service supports up to 256 configured principals/connections, at most four
connections per UID so idle peers cannot monopolize the pool, an 8 MiB view
cache, an 8 MiB aggregate output queue, and the bounded `raven.session/v1`
envelope. Each authorized response has a 2,000-row-per-list limit; larger views
require a narrower grant. Aggregate client reads are limited to 32/second with a
burst of 64. Excess reads receive a retry-later diagnostic and never increase
scheduler collection. Slow clients disconnect without blocking other peers.

## Operations and qualification gates

For administrator diagnostics, run in the foreground first and retain stderr in
the service manager's protected journal. Failed parser/ownership checks, policy
errors and rejected publications are explicit. Client envelopes expose source
ages, stale state and replay gaps. An exclusive kernel lock prevents two owners
from collecting into the same directory; stale PID numbers grant no ownership.

Use a user/system service manager only after validating group membership, local
filesystem behavior, scheduler privacy settings, rate limits, account mapping and
revocation at the target site. No production installation is performed by this
implementation. Cross-UID fixture evidence is distinct from a site security
review. Scheduler privileges, federation, directory services and real user
entitlements require administrator qualification.

Upgrade the service and clients together for protocol changes. Stop the old
foreground service or its owned service-manager unit, install the reviewed
release, then restart with the same explicit policy. To roll back, stop the shared
service and use ordinary Raven connections; no scheduler state has been changed.
Keep protected state for diagnosis, and remove its directory only after verifying
the owner is stopped. The service never cancels or adopts allocations on exit.
