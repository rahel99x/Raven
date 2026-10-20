"""Embedded terminal runtime: VT state, bounded IPC, PTY ownership and recordings.

The runtime has no controller, views or curses dependency. UI integration owns
focus and permission decisions; the broker owns its child PTYs.
"""
