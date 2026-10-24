"""Trace workspace terminal API; capture and index I/O stay in background owners."""


def initialize(state):
    from .workspace_ui_state import initialize as owner

    return owner(state)


def publish(state, result):
    from .workspace_ui_state import publish as owner

    return owner(state, result)


def query_options(state):
    from .workspace_ui_state import query_options as owner

    return owner(state)


def query_requests(state):
    from .workspace_ui_state import query_requests as owner

    return owner(state)


def command(app, state, args):
    from .workspace_ui_commands import command as owner

    return owner(app, state, args)


def render(views, state, job_id, width):
    from .workspace_ui_layout import render as owner

    return owner(views, state, job_id, width)


def detail_request(state):
    from .workspace_ui_state import detail_request as owner

    return owner(state)


def symbol_request(state):
    from .workspace_ui_state import symbol_request as owner

    return owner(state)


def save_preferences(state):
    from .workspace_ui_state import save_preferences as owner

    return owner(state)


def restore_preferences(state, value):
    from .workspace_ui_state import restore_preferences as owner

    return owner(state, value)
