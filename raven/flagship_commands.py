"""Compatibility access to canonical task workspace descriptors.

Task discovery, toolbar workspaces and generated guides share command_registry.
Execution remains with the reviewed operation handlers.
"""
from .command_registry import Task, TASKS, task, operation_keys, task_rows as command_rows

__all__ = ["Task", "TASKS", "task", "operation_keys", "command_rows"]
