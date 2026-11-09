"""A Language Server Protocol implementation for the Gable language.

The server is intentionally thin: it owns protocol framing, document
synchronisation and scheduling, and delegates all language knowledge to the
``gable`` command line compiler. Diagnostics are lifted from ``gable check``
output; formatting is produced by ``gable fmt`` applied to a throw-away copy
of the buffer.
"""

__version__ = "0.1.0"
