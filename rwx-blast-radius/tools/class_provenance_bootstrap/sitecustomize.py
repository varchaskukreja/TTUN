"""Opt-in independent baseline class-call provenance probe."""
import os
if os.environ.get('CLASS_PROVENANCE_SOURCE'):
    from class_provenance_probe import start
    start()
