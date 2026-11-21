"""Experimental baseline spawn capture. Receipts are evidence, never skip permission.

Use install_parent(context_getter, source, output) before creating spawn pools.
Fresh interpreters load the separate comparison_bootstrap/sitecustomize.py.
No target source files are modified. All unassigned worker activity is retained
under a PID context that must expand to every job observed in that worker.
"""
import atexit
from collections import defaultdict
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
            if _state.get('launch'):
                write_event('popen_child_end', token=_state['launch']['token'], context=_state['context'])


def bootstrap():
    global _state
    if _state is not None:
        return
    argv = sys.orig_argv
    position = argv.index('-c') + 1 if '-c' in argv else len(argv)
    if position < len(argv) and re.fullmatch(r'from multiprocessing\.resource_tracker import main;main\([0-9]+\)', argv[position]):
        return  # Standard tracker executes no target/user jobs.
    # Do not activate comparison_types' pytest plugin in worker interpreters.
    previous = os.environ.pop('COMPARISON_SOURCE', None)
    try:
        from comparison_types import Recorder, Finder
    finally:
        if previous is not None:
            os.environ['COMPARISON_SOURCE'] = previous
    from comparison_subprocess import consume_bootstrap_identity
    launch = consume_bootstrap_identity()
    context = launch['context'] if launch else f'@worker-unassigned:{os.getpid()}'
    _state = {'context': context, 'unassigned': context, 'contexts': {context} if launch else set(), 'launch': launch}
    recorder = Recorder(lambda: _state['context'], root=os.environ['COMPARISON_SPAWN_SOURCE'])
    _state['recorder'] = recorder
    finder = Finder(os.environ['COMPARISON_SPAWN_SOURCE'], recorder, ('sqlglot/',))
    _state['finder'] = finder
    finder.record_preexisting()
    recorder.readers.start()
    sys.meta_path.insert(0, finder)
    write_event('worker_start', argv=[] if launch else sys.orig_argv)
    if launch:
        write_event('popen_child_start', **launch)
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
    from comparison_subprocess import install_popen
    restore_popen, tracked_popen_audit = install_popen(context_getter, write_event)
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
            if not tracked_popen_audit(event, args):
                write_event('untracked_activity', event=event, context=context_getter())
    sys.addaudithook(audit)
    def restore():
        active[0] = False
        restore_popen()
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
    """Index receipts once and parse each cumulative PID snapshot exactly once."""
    directory = Path(directory)
    events, errors = [], []
    indexed = defaultdict(lambda: defaultdict(list))
    by_kind = defaultdict(list)
    for path in directory.glob('events-*.jsonl'):
        try:
            for line in path.read_text().splitlines():
                event = json.loads(line)
                if type(event) is not dict or type(event.get('kind')) is not str or type(event.get('pid')) is not int:
                    raise ValueError('invalid event')
                events.append(event)
                by_kind[event['kind']].append(event)
                key = event.get('job', event.get('token', event['pid']))
                indexed[event['kind']][key].append(event)
        except (OSError, ValueError, TypeError):
            errors.append(f'invalid receipt shard: {path.name}')
    retain, workers = set(), defaultdict(set)
    submits = indexed['submitted']
    launches = indexed['popen_launch']
    all_contexts = {e.get('context', '') for e in by_kind['submitted'] + by_kind['popen_launch']}
    valid_shards = {}

    def shard_complete(pid):
        if pid not in valid_shards:
            try:
                shard = json.loads((directory / f'observations-{pid}.json').read_text())
                valid_shards[pid] = (type(shard.get('sites')) is dict and type(shard.get('read_dependencies')) is dict
                    and len(indexed['worker_start'][pid]) == 1 and len(indexed['worker_end'][pid]) == 1)
            except (OSError, ValueError, AttributeError):
                valid_shards[pid] = False
        return valid_shards[pid]

    def one(kind, key):
        values = indexed[kind][key]
        return values[0] if len(values) == 1 else None

    for job, submitted_events in submits.items():
        submitted = submitted_events[0]
        context = submitted.get('context', '')
        start, end = one('job_start', job), one('job_end', job)
        if len(submitted_events) != 1 or not submitted.get('supported') or not start or not end:
            retain.add(context); errors.append(f'incomplete job {job}'); continue
        pid = start['pid']
        workers[str(pid)].add(context)
        if end['pid'] != pid or start.get('context') != context or end.get('context') != context or end.get('returned') is not True or not shard_complete(pid):
            retain.add(context); errors.append(f'inconsistent job {job}')
    for kind in ('job_start', 'job_end'):
        if set(indexed[kind]) - set(submits):
            errors.append('job receipt without submission')

    tracked_pids = {e['child_pid'] for e in by_kind['process_start'] if e.get('tracked') is True}
    for token, launch_events in launches.items():
        launch = launch_events[0]
        context = launch.get('context', '')
        started, exited = one('popen_started', token), one('popen_exit', token)
        child_start, child_end = one('popen_child_start', token), one('popen_child_end', token)
        if len(launch_events) != 1 or not all((started, exited, child_start, child_end)) or indexed['popen_failed'][token]:
            retain.add(context); errors.append(f'incomplete Python launch {token}'); continue
        pid = started['child_pid']
        tracked_pids.add(pid)
        workers[str(pid)].add(context)
        if (child_start['pid'] != pid or child_end['pid'] != pid or exited['child_pid'] != pid
                or any(e.get('context') != context for e in (started, exited, child_start, child_end))
                or child_start.get('identity_matched') is not True or child_start.get('identity') != launch.get('identity')
                or exited.get('returncode') != 0 or not shard_complete(pid)):
            retain.add(context); errors.append(f'inconsistent Python launch {token}')
    for kind in ('popen_started', 'popen_exit', 'popen_child_start', 'popen_child_end', 'popen_failed'):
        if set(indexed[kind]) - set(launches):
            errors.append('Python receipt without launch')
    for event in by_kind['worker_start']:
        if event['pid'] not in tracked_pids:
            argv = event.get('argv', [])
            position = argv.index('-c') + 1 if '-c' in argv else len(argv)
            infrastructure = position < len(argv) and re.fullmatch(
                r'from multiprocessing\.resource_tracker import main;main\([0-9]+\)', argv[position]) is not None
            if not infrastructure:
                errors.append(f"untracked bootstrap PID {event['pid']}")
    for event in by_kind['untracked_activity'] + [e for e in by_kind['process_start'] if not e.get('tracked')]:
        context = event.get('context', '')
        errors.append(f"untracked child activity in {context or '<collection>'}")
        if context.startswith('@worker-unassigned:'):
            retain.update(workers.get(str(event['pid']), ()))
        elif not context:
            retain.update(all_contexts)
        else:
            retain.add(context)
    if errors:
        retain.update(all_contexts)
    return {'submitted': len(submits), 'python_subprocesses': len(launches), 'must_retain': sorted(retain),
            'errors': sorted(set(errors)), 'worker_contexts': {pid: sorted(contexts) for pid, contexts in workers.items()},
            'parsed_observation_shards': len(valid_shards), 'untracked_child_activity_checked': True,
            'activity_audit_exhaustive': False, 'narrowing_authorized': False}
