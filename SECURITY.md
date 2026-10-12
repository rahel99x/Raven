# Security policy

Please report suspected vulnerabilities privately through GitHub's
[security advisory form](https://github.com/rahel99x/Raven/security/advisories/new)
when private reporting is enabled. If the form is unavailable, open an issue asking
for a private contact channel without including exploit details or sensitive data.
Include the affected revision, a minimal reproduction using synthetic data, and
the expected impact. Fixes target the latest main-branch version.

Raven runs with your user permissions and existing Slurm/SSH access. Configuration
notification hooks and Python plugins are executable code: only load trusted ones.
SSH uses your normal authentication and host-key verification. Do not commit
credentials, personal configuration, private job output, or cluster recordings.

Plain-text reports, JSON/CSV exports, and recordings can contain job names, users,
resource usage, paths, and logs. Review them before sharing. Use the simulated
cluster for public examples. Job mutations require interactive confirmation or
explicit `--yes` in scripted mode; unattended scripts should grant that deliberately.
