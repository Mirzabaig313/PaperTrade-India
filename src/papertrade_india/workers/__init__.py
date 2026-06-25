"""Background loops that drive the broker.

Workers may import ``broker``, but never reach into subsystems
directly. They are deliberately thin: most logic stays on the broker
facade so the same behavior is available via direct calls in tests.
"""
