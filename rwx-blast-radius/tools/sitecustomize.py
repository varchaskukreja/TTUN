"""Trace spawned baseline test workers with their parent's test identity."""
import os

if os.environ.get('BLAST_CONTEXT') and os.environ.get('BLAST_WORK'):
    import atexit
    import json
    import sys
    from pathlib import Path
    from multiprocessing.util import Finalize
    import coverage

    _root = Path(os.environ['BLAST_SOURCE']).resolve()
    _work = Path(os.environ['BLAST_WORK']).resolve()
    _context = os.environ['BLAST_CONTEXT']
    _reads = {}
    _cov = coverage.Coverage(data_file=str(_work / 'baseline.coverage'), data_suffix=True,
                             source=[str(_root / 'sqlglot'), str(_root / 'tests')], context='')
    _cov.start()
    _cov.switch_context(_context)
    def _audit(event, args):
        if event == 'open' and args and isinstance(args[0], (str, bytes)):
            path = os.path.abspath(os.fsdecode(args[0]))
            if path.startswith(str(_root) + os.sep) and not path.endswith('.pyc'):
                _reads.setdefault(os.path.relpath(path, _root), set()).add(_context)
    sys.addaudithook(_audit)
    _classes = None
    if os.environ.get('BLAST_CLASSES'):
        from class_dependencies import ClassDependencies
        _classes = ClassDependencies(_root, lambda: _context)
        _classes.start()
    _saved = False
    def _save():
        global _saved
        if _saved:
            return
        _saved = True
        if _classes is not None:
            _classes.stop()
            (_work / f'child-classes-{os.getpid()}.json').write_text(json.dumps(_classes.snapshot()))
        _cov.stop()
        _cov.save()
        (_work / f'child-reads-{os.getpid()}.json').write_text(json.dumps({p: sorted(contexts) for p, contexts in _reads.items()}))
    atexit.register(_save)
    Finalize(None, _save, exitpriority=0)
