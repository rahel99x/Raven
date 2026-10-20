#!/usr/bin/env python3
"""Check all core names and the actual migrated typing/layer/format boundaries."""

from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    original_paths = ["raven/transport", "raven/command_handlers.py"]
    migrated_paths = [
        "raven/terminal",
        "raven/jobs",
        "raven/job_logs",
        "raven/recordings",
        "raven/execution",
        "raven/entry",
        "raven/tracing",
        "raven/launching",
        "raven/operation_adapters",
        "raven/operation_support.py",
        "raven/operations.py",
        "raven/background_tasks.py",
        "raven/table_query.py",
        "raven/series_presentation.py",
        "raven/heavy",
        "raven/ui",
        "raven/installation",
        "raven/configuration",
        "raven/collection",
        "raven/raven_allocation.py",
        "raven/raven_allocation_adapters.py",
        "raven/raven_allocation_helper.py",
        "raven/raven_allocation_identity.py",
        "raven/raven_allocation_ownership.py",
        "raven/raven_allocation_recovery.py",
        "raven/raven_worker.py",
        "raven/raven_worker_client.py",
        "raven/raven_fork_task.py",
        "raven/raven_protocol.py",
        "raven/raven_limits.py",
        "raven/research_text.py",
        "raven/research_context.py",
        "raven/planning_catalog.py",
        "raven/planning_input.py",
        "raven/planning_records.py",
        "raven/array_ranges.py",
        "raven/recording_settings_port.py",
        "raven/science_analysis.py",
        "raven/ops_workflow.py",
        "raven/log_paths.py",
        "raven/view_catalog.py",
        "raven/input_keys.py",
        "raven/hostlist.py",
        "raven/operation_json.py",
        "raven/user_identity.py",
        "raven/placement_pids.py",
        "raven/connection_scope.py",
        "raven/cache_budget.py",
        "raven/local_files.py",
        "raven/remote_files.py",
        "raven/project_files.py",
        "raven/source_budget.py",
        "raven/execution_admission.py",
        "raven/execution_policy.py",
        "raven/operation_context.py",
        "raven/bounded_command.py",
        "raven/hooks.py",
        "raven/redaction.py",
        "raven/interaction.py",
        "raven/chart_interaction.py",
        "raven/scrollbars.py",
        "raven/pane_drag.py",
        "raven/workspace_layout.py",
        "raven/history_browser.py",
        "raven/metric_live.py",
        "raven/grouping_toolbar.py",
        "raven/manual_group_ui.py",
        "raven/table_ui.py",
        "raven/table_tools.py",
        "raven/recent_history.py",
        "raven/workbench.py",
        "raven/metric_sampling_runtime.py",
        "raven/cadence.py",
        "raven/refresh_preferences.py",
        "scripts/check_import_layers.py",
        "scripts/check_quality.py",
        "scripts/benchmark_ui_dispatch.py",
    ]
    # Every artifact under this historical directory is Markdown; raw captures
    # belong on the archive reference, not in the active source/package tree.
    historical = root / "docs/implementation"
    raw = [
        path
        for path in historical.rglob("*")
        if path.is_file() and path.suffix.lower() != ".md"
    ]
    if raw:
        print(
            "Historical raw evidence belongs on the evidence reference: " + str(raw[0]),
            file=sys.stderr,
        )
        return 1
    commands = [
        [sys.executable, "-m", "ruff", "check", "--select", "F", "raven"],
        [
            str(Path(sys.executable).parent / "lint-imports"),
            "--config",
            ".importlinter",
        ],
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--config",
            "ruff-boundary.toml",
            *original_paths,
        ],
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            "--config",
            "ruff-boundary.toml",
            *original_paths,
        ],
        [sys.executable, "-m", "ruff", "format", "--check", *migrated_paths],
        [sys.executable, "-m", "mypy", "--config-file", "mypy-boundary.ini"],
        [sys.executable, "scripts/check_import_layers.py", "--require-acyclic"],
    ]
    for command in commands:
        try:
            completed = subprocess.run(command, cwd=root, check=False)
        except FileNotFoundError:
            print(
                "Install Raven's development dependencies: python -m pip install -e '.[dev]'",
                file=sys.stderr,
            )
            return 2
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
