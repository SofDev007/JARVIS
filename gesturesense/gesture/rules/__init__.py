"""Gesture rule package.

Importing this package loads every rule module so their
:func:`~gesturesense.gesture.base.register_gesture` decorators execute.
Drop a new module here (and import it below) to add gestures — nothing else
in the codebase changes.
"""

from gesturesense.gesture.rules import palm, pointing, symbols, thumbs  # noqa: F401
