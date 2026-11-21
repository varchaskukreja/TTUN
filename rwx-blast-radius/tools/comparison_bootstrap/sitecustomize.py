"""Opt-in baseline comparison worker instrumentation, before spawn unpickling."""
import os
if os.environ.get('COMPARISON_SPAWN_DIR'):
    from comparison_spawn import bootstrap
    bootstrap()
