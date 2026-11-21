"""Experimental baseline spawn capture. Receipts are evidence, never skip permission.

Use install_parent(context_getter, source, output) before creating spawn pools.
Fresh interpreters load the separate comparison_bootstrap/sitecustomize.py.
No target source files are modified. All unassigned worker activity is retained
under a PID context that must expand to every job observed in that worker.
"""
import atexit
import json
import os
import re
from pathlib import Path
import sys
import uuid

_state = None
_parent_getter = None
_original_submit = None
_final_saved = False


def write_event(kind, **fields):
    directory = Path(os.environ['COMPARISON_SPAWN_DIR'])
    directory.mkdir(parents=True, exist_ok=True)
    record = dict(kind=kind, pid=os.getpid(), **fields)
    # Separate append-only file per PID; each receipt is a single append write.
    with (directory / f'events-{os.getpid()}.jsonl').open('a') as stream:
        stream.write(json.dumps(record, sort_keys=True) + '\n')


def save_snapshot(final=False):
    global _final_saved
    if final and _final_saved:
        return
    if _state is None:
        return
    recorder = _state['recorder']
    with recorder.readers.muted():
        snapshot = recorder.snapshot()
        path = Path(os.environ['COMPARISON_SPAWN_DIR']) / f'observations-{os.getpid()}.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(snapshot, separators=(',', ':'), sort_keys=True))
        temporary.replace(path)
        if final:
            _final_saved = True
            write_event('worker_end', contexts=sorted(_state['contexts']))


def bootstrap():
    global _state
    if _state is not None:
        return
    # Do not activate comparison_types' pytest plugin in worker interpreters.
    previous = os.environ.pop('COMPARISON_SOURCE', None)
    try:
        from comparison_types import Recorder, Finder
    finally:
        if previous is not None:
            os.environ['COMPARISON_SOURCE'] = previous
    context = f'@worker-unassigned:{os.getpid()}'
    _state = {'context': context, 'unassigned': context, 'contexts': set()}
    recorder = Recorder(lambda: _state['context'], root=os.environ['COMPARISON_SPAWN_SOURCE'])
    _state['recorder'] = recorder
    finder = Finder(os.environ['COMPARISON_SPAWN_SOURCE'], recorder, ('sqlglot/',))
    _state['finder'] = finder
    finder.record_preexisting()
    recorder.readers.start()
    sys.meta_path.insert(0, finder)
    write_event('worker_start', argv=sys.orig_argv)
    watch_activity(lambda: _state['context'], allow_pool=False)
    atexit.register(save_snapshot, True)
    from multiprocessing.util import Finalize
    Finalize(None, save_snapshot, args=(True,), exitpriority=-sys.maxsize)


def run_job(job, context, fn, args, kwargs):
    if _state is None:
        raise RuntimeError('comparison bootstrap missing')
    _state['context'] = context
    _state['contexts'].add(context)
    write_event('job_start', job=job, context=context)
    success = False
    try:
        value = fn(*args, **kwargs)
        success = True
        return value
    finally:
        save_snapshot()
        write_event('job_end', job=job, context=context, returned=success)
        _state['context'] = _state['unassigned']


def watch_activity(context_getter, allow_pool):
    from multiprocessing.process import BaseProcess
    original = BaseProcess.start
    active = [True]
    def start(process):
        target = getattr(process, '_target', None)
        allowed = (allow_pool and getattr(target, '__module__', None) == 'concurrent.futures.process'
                   and getattr(target, '__name__', None) == '_process_worker'
                   and process._start_method == 'spawn')
        result = original(process)
        write_event('process_start', child_pid=process.pid, context=context_getter(), tracked=allowed)
        return result
    BaseProcess.start = start
    def audit(event, args):
        if active[0] and event in ('subprocess.Popen', 'os.system', 'os.fork', 'os.posix_spawn'):
            write_event('untracked_activity', event=event, context=context_getter())
    sys.addaudithook(audit)
    def restore():
        active[0] = False
        BaseProcess.start = original
    return restore


def install_parent(context_getter, source, output):
    global _parent_getter, _original_submit
    if _original_submit is not None:
        raise RuntimeError('already installed')
    from concurrent.futures import ProcessPoolExecutor
    _parent_getter = context_getter
    os.environ['COMPARISON_SPAWN_SOURCE'] = str(Path(source).resolve())
    os.environ['COMPARISON_SPAWN_DIR'] = str(Path(output).resolve())
    tools = Path(__file__).resolve().parent
    os.environ['PYTHONPATH'] = os.pathsep.join([str(tools / 'comparison_bootstrap'), str(tools), os.environ.get('PYTHONPATH', '')])
    restore_activity = watch_activity(context_getter, allow_pool=True)
    _original_submit = ProcessPoolExecutor.submit
    def submit(executor, fn, /, *args, **kwargs):
        context = context_getter()
        job = uuid.uuid4().hex
        supported = executor._mp_context.get_start_method() == 'spawn'
        write_event('submitted', job=job, context=context, supported=supported)
        if not supported:
            return _original_submit(executor, fn, *args, **kwargs)
        return _original_submit(executor, run_job, job, context, fn, args, kwargs)
    ProcessPoolExecutor.submit = submit
    def restore():
        global _original_submit
        ProcessPoolExecutor.submit = _original_submit
        _original_submit = None
        restore_activity()
    return restore


def audit_receipts(directory):
    """Conservative evidence summary; callers must additionally handle untracked activity."""
    events = []
    errors = []
    for path in Path(directory).glob('events-*.jsonl'):
        try:
            events.extend(json.loads(line) for line in path.read_text().splitlines())
        except (OSError, ValueError):
            errors.append(f'invalid receipt shard: {path.name}')
    submitted_events = [e for e in events if e['kind'] == 'submitted']
    submits = {e['job']: e for e in submitted_events}
    if len(submits) != len(submitted_events):
        errors.append('duplicate submitted job IDs')
    if any(e['kind'] in ('job_start', 'job_end') and e['job'] not in submits for e in events):
        errors.append('job receipt without submission')
    retain = set()
    workers = {}
    for job, submitted in submits.items():
        starts = [e for e in events if e['kind'] == 'job_start' and e['job'] == job]
        ends = [e for e in events if e['kind'] == 'job_end' and e['job'] == job]
        if not submitted['supported'] or len(starts) != 1 or len(ends) != 1:
            retain.add(submitted['context']); continue
        start, end = starts[0], ends[0]
        pid = start['pid']
        workers.setdefault(str(pid), set()).add(submitted['context'])
        if end['pid'] != pid or start['context'] != submitted['context'] or end['context'] != submitted['context'] or not end['returned']:
            retain.add(submitted['context'])
        if not any(e['kind'] == 'worker_start' and e['pid'] == pid for e in events):
            retain.add(submitted['context'])
        try:
            shard = json.loads((Path(directory) / f'observations-{pid}.json').read_text())
            if not isinstance(shard.get('sites'), dict) or not isinstance(shard.get('read_dependencies'), dict):
                raise ValueError('invalid observation schema')
        except (OSError, ValueError, AttributeError):
            retain.add(submitted['context'])
        if not any(e['kind'] == 'worker_end' and e['pid'] == pid for e in events) or not (Path(directory) / f'observations-{pid}.json').exists():
            retain.add(submitted['context'])
    # Bootstrap-only infrastructure is allowed only for the exact standard
    # multiprocessing resource tracker command, which executes no user jobs.
    tracked_pids = {e['child_pid'] for e in events if e['kind'] == 'process_start' and e['tracked']}
    for event in events:
        if event['kind'] == 'worker_start' and event['pid'] not in tracked_pids:
            argv = event.get('argv', [])
            position = argv.index('-c') + 1 if '-c' in argv else len(argv)
            infrastructure = (position < len(argv) and re.fullmatch(
                r'from multiprocessing\.resource_tracker import main;main\([0-9]+\)', argv[position]) is not None)
            if not infrastructure:
                errors.append(f"untracked bootstrap PID {event['pid']}")
        if event['kind'] == 'untracked_activity' or (event['kind'] == 'process_start' and not event['tracked']):
            context = event['context']
            if context.startswith('@worker-unassigned:'):
                retain.update(workers.get(str(event['pid']), ()))
            elif not context:
                retain.update(e['context'] for e in submits.values())
            else:
                retain.add(context)
    if errors:
        retain.update(e['context'] for e in submits.values())
    return {'submitted': len(submits), 'must_retain': sorted(retain), 'errors': errors,
            'worker_contexts': {pid: sorted(contexts) for pid, contexts in workers.items()},
            'untracked_child_activity_checked': True, 'activity_audit_exhaustive': False, 'narrowing_authorized': False}
