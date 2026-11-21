#!/usr/bin/env python3
"""Optional baseline-only comparison TYPE observations, never production selection.

Certificates establish equivalence for observed operand types only. They do not
prove completeness, deterministic future executions, absence of concurrent type
mutation, or source/bytecode/code-object-inspection equivalence. Instrumentation also changes stack
frames, timing, globals and operand reference lifetimes. Keep source readers and
unobserved tests conservative; never treat a pilot certificate as a skip list.

Python semantics: reference/datamodel.html#object.__eq__ and
reference/expressions.html#comparisons on docs.python.org. Rich comparisons can
return arbitrary objects, dispatch the subclass first, and override __ne__
independently. Enum subclasses of str/int are not identity equality types.
"""
from __future__ import annotations
import argparse
import ast
from contextlib import contextmanager
import enum
import hashlib
import importlib.abc
import importlib.util
import difflib
import io
import tokenize
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import threading
import time
import types

SUPPORTED = (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)
TYPE_DICT = type.__dict__['__dict__']
TYPE_MRO = type.__dict__['__mro__']
TYPE_QUALNAME = type.__dict__['__qualname__']
PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'


def namespace(cls):
    # Direct builtin descriptors bypass custom metaclass __getattribute__.
    return TYPE_DICT.__get__(cls, type(cls))


def effective_slot(cls, name):
    for ancestor in TYPE_MRO.__get__(cls, type(cls)):
        values = namespace(ancestor)
        if name in values:
            return values[name]
    return None


def describe_type(cls):
    mro = TYPE_MRO.__get__(cls, type(cls))
    values = namespace(cls)
    module = values.get('__module__', 'builtins')
    if type(module) is not str:
        module = '<non-string module>'
    meta = type(cls)
    standard_meta = meta is type or meta is enum.EnumType
    custom_metaclass_object = cls is not type and any(base is type for base in mro)
    # CPython3.13 NoneType owns distinct descriptors but uses the same C
    # _Py_BaseObject_RichCompare implementation (Objects/object.c).
    none_identity = cls is type(None) and sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 13)
    return {'module': module, 'qualname': TYPE_QUALNAME.__get__(cls, meta),
            'eq': 'object_identity' if effective_slot(cls, '__eq__') is object.__eq__ else 'none_identity' if none_identity else 'overridden',
            'ne': 'object_identity' if effective_slot(cls, '__ne__') is object.__ne__ else 'none_identity' if none_identity else 'overridden',
            'none_type': cls is type(None),
            'standard_enum_type': meta is enum.EnumType and any(base is enum.Enum for base in mro),
            'supported_metaclass': standard_meta and not custom_metaclass_object}


def describe_operand(value):
    cls = type(value)
    descriptor = describe_type(cls)
    descriptor.update(backed_enum_kind='', canonical_member=False, builtin_values_bijective=False)
    if not descriptor['standard_enum_type'] or not descriptor['supported_metaclass']:
        return descriptor
    mro = TYPE_MRO.__get__(cls, type(cls))
    builtin = str if any(base is str for base in mro) else int if any(base is int for base in mro) else None
    if builtin is None or effective_slot(cls, '__eq__') is not builtin.__eq__ or effective_slot(cls, '__ne__') is not builtin.__ne__:
        return descriptor
    members = namespace(cls).get('_member_map_')
    if type(members) is not dict:
        return descriptor
    descriptor['backed_enum_kind'] = builtin.__name__
    seen = {}
    valid = True
    # Invoke exact builtin conversion implementations, never member hooks.
    for member in members.values():
        if type(member) is not cls:
            valid = False
            break
        if member is value:
            descriptor['canonical_member'] = True
        raw = str.__str__(member) if builtin is str else int.__int__(member)
        if type(raw) is not builtin:
            valid = False
            break
        previous = seen.get(raw)
        if previous is not None and previous is not member:
            valid = False
            break
        seen[raw] = member
    descriptor['builtin_values_bijective'] = valid and bool(seen)
    return descriptor


def site_key(path, node):
    return f'{path}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}'


def code_key(path, code):
    return f'{path}:{code.co_firstlineno}:{code.co_qualname}'


def code_metadata(path, code):
    lines = [line for _, _, line in code.co_lines() if line is not None]
    return {'path': path, 'name': code.co_name, 'qualname': code.co_qualname,
            'first_line': code.co_firstlineno,
            'span_first_line': min(lines) if lines else code.co_firstlineno,
            'span_last_line': max(lines) if lines else code.co_firstlineno}


class ReaderAudit:
    """Conservative dependency observations, NOT exhaustive reflection detection.

    CPython audits acquisition of function.__code__/frame.f_code, but direct
    co_code/co_consts reads of an already-acquired code object are invisible.
    Collection acquisitions are treated as escapes requiring all tests retained.
    """
    def __init__(self, root, context=lambda: ''):
        self.root = Path(root).resolve()
        self.context = context
        self.source, self.code = {}, {}
        self.code_objects = {}
        self.active = False
        self.installed = False
        self.local = threading.local()

    @contextmanager
    def muted(self):
        depth = getattr(self.local, 'depth', 0)
        self.local.depth = depth + 1
        try:
            yield
        finally:
            self.local.depth = depth

    def start(self):
        self.active = True
        if not self.installed:
            sys.addaudithook(self.audit)
            self.installed = True

    def stop(self):
        self.active = False

    def record(self, filename, target):
        path = Path(filename).resolve()
        if path.is_relative_to(self.root) and path.suffix == '.py':
            relative = path.relative_to(self.root).as_posix()
            target.setdefault(relative, set()).add(self.context())

    def record_code(self, code):
        # Acquiring an outer code object also exposes every nested code object
        # through co_consts; retaining descendants avoids that reflection gap.
        pending, seen = [code], set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            path = Path(current.co_filename).resolve()
            if path.is_relative_to(self.root) and path.suffix == '.py':
                relative = path.relative_to(self.root).as_posix()
                key = code_key(relative, current)
                record = self.code_objects.setdefault(key, dict(code_metadata(relative, current), contexts=set()))
                record['contexts'].add(self.context())
                self.code.setdefault(relative, set()).add(self.context())
            pending.extend(value for value in current.co_consts if type(value) is types.CodeType)

    def record_frame(self, frame):
        # Acquiring a frame exposes f_lasti/f_lineno and f_back without any
        # subsequent f_code audit. Include every accessible ancestor frame.
        while frame is not None:
            self.record_code(object.__getattribute__(frame, 'f_code'))
            frame = object.__getattribute__(frame, 'f_back')

    def audit(self, event, args):
        if not self.active or getattr(self.local, 'depth', 0):
            return
        if event not in ('open', 'object.__getattr__', 'sys._getframe'):
            return
        with self.muted():
            if event == 'sys._getframe' and args and type(args[0]) is types.FrameType:
                self.record_frame(args[0])
            elif event == 'open' and args and isinstance(args[0], (str, bytes)):
                if len(args) < 3 or not (args[2] & os.O_WRONLY):
                    self.record(os.fsdecode(args[0]), self.source)
            elif event == 'object.__getattr__' and len(args) == 2:
                obj, name = args
                code = None
                if name == '__code__' and type(obj) is types.FunctionType:
                    code = object.__getattribute__(obj, '__code__')
                elif name == 'f_code' and type(obj) is types.FrameType:
                    code = object.__getattribute__(obj, 'f_code')
                elif name == 'tb_frame' and type(obj) is types.TracebackType:
                    self.record_frame(object.__getattribute__(obj, 'tb_frame'))
                elif name in ('gi_code', 'cr_code', 'ag_code') and type(obj) in (types.GeneratorType, types.CoroutineType, types.AsyncGeneratorType):
                    code = object.__getattribute__(obj, name)
                if code is not None:
                    self.record_code(code)

    def snapshot(self):
        return {'source': {path: sorted(contexts) for path, contexts in self.source.items()},
                'code': {path: sorted(contexts) for path, contexts in self.code.items()},
                'code_objects': {key: dict(record, contexts=sorted(record['contexts'])) for key, record in self.code_objects.items()},
                'code_object_granularity': True,
                'coverage_complete': False}


class Recorder:
    def __init__(self, context=lambda: '', root=None):
        self.context = context
        self.readers = ReaderAudit(root, context) if root is not None else None
        self.sites = {}
        self.pairs = {}
        self.execution_codes = {}
        self.code_ancestry = {}
        self.preexisting_modules = {}
        self.serialization_cache = {}

    def compare(self, site, operation, left, right):
        # No operand values or comparison results are stored. Type slot lookups
        # are repeated: a monkeypatch between observations must not hit a cache.
        if self.readers:
            with self.readers.muted():
                caller = sys._getframe(1).f_code
        else:
            caller = sys._getframe(1).f_code
        path = self.sites[site]['path']
        owner = code_key(path, caller)
        self.execution_codes.setdefault(site, {}).setdefault(owner, set()).add(self.context())
        left_desc, right_desc = describe_operand(left), describe_operand(right)
        for descriptor in (left_desc, right_desc):
            if type(descriptor['qualname']) is not str:
                descriptor['qualname'] = str.__str__(descriptor['qualname'])
        primitive_key = (tuple(left_desc.values()), tuple(right_desc.values()), type(left) is type(right))
        key = self.serialization_cache.get(primitive_key)
        if key is None:
            pair = {'left': left_desc, 'right': right_desc, 'same_type': type(left) is type(right)}
            key = json.dumps(pair, sort_keys=True, separators=(',', ':'))
            if len(self.serialization_cache) >= 4096:
                self.serialization_cache.clear()
            self.serialization_cache[primitive_key] = key
        self.pairs.setdefault(site, {}).setdefault(key, set()).add(self.context())
        # Execute Python's original operation, preserving dispatch and its exact
        # return object. Never coerce a rich comparison result with bool().
        if operation == 'Is':
            return left is right
        if operation == 'IsNot':
            return left is not right
        if operation == 'Eq':
            return left == right
        if operation == 'NotEq':
            return left != right
        raise ValueError(operation)

    def compile(self, source, path, filename=None):
        raw = source.encode() if isinstance(source, str) else source
        filename = filename or path
        tree = compile(raw, filename, 'exec', ast.PyCF_ONLY_AST)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
        names.update(node.name for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))
        names.update(node.asname or node.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.alias))
        names.update(node.name for node in ast.walk(tree) if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name)
        names.update(node.rest for node in ast.walk(tree) if isinstance(node, ast.MatchMapping) and node.rest)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                names.update(node.names)
        helper = '_blast_comparison_observe'
        while helper in names:
            helper += '_'
        recorder = self
        digest = hashlib.sha256(raw).hexdigest()
        class Transform(ast.NodeTransformer):
            def visit_Compare(self, node):
                original = node
                node = self.generic_visit(node)
                if len(node.ops) != 1 or len(node.comparators) != 1 or not isinstance(node.ops[0], SUPPORTED):
                    return node
                site = site_key(path, original)
                operation = type(node.ops[0]).__name__
                recorder.sites[site] = {'path': path, 'line': original.lineno, 'column': original.col_offset,
                                        'end_line': original.end_lineno, 'end_column': original.end_col_offset,
                                        'operator': operation, 'source_sha256': digest}
                call = ast.Call(func=ast.Name(id=helper, ctx=ast.Load()),
                                args=[ast.Constant(site), ast.Constant(operation), node.left, node.comparators[0]], keywords=[])
                return ast.copy_location(call, original)
        transformed = ast.fix_missing_locations(Transform().visit(tree))
        compiled = compile(transformed, filename, 'exec')
        def register_codes(code, ancestors=()):
            record = dict(code_metadata(path, code), key=code_key(path, code))
            chain = (*ancestors, record)
            self.code_ancestry[record['key']] = list(chain)
            for constant in code.co_consts:
                if type(constant) is types.CodeType:
                    register_codes(constant, chain)
        if self.readers:
            with self.readers.muted():
                register_codes(compiled)
        else:
            register_codes(compiled)
        return compiled, helper

    def execute(self, source, path='synthetic.py', globals_=None):
        target = {} if globals_ is None else globals_
        code, helper = self.compile(source, path)
        target[helper] = self.compare
        exec(code, target)
        return target

    def snapshot(self):
        sites = {}
        for site, metadata in self.sites.items():
            observations = []
            for serialized, contexts in sorted(self.pairs.get(site, {}).items()):
                observations.append(dict(json.loads(serialized), contexts=sorted(contexts)))
            executions = {key: {'contexts': sorted(contexts), 'ancestry': self.code_ancestry.get(key, [])} for key, contexts in self.execution_codes.get(site, {}).items()}
            sites[site] = dict(metadata, observations=observations, execution_codes=executions)
        return {'format': 1, 'kind': 'baseline_operand_types', 'sites': sites,
                'read_dependencies': self.readers.snapshot() if self.readers else {},
                'preexisting_modules': dict(sorted(self.preexisting_modules.items())),
                'ineligible_files': sorted(set(self.preexisting_modules.values())),
                'limitations': ['observed executions only', 'source/code-object reader coverage is observational, not complete',
                                'cached code objects acquired before the audit hook are invisible',
                                'subprocess executions are not observed by this pilot',
                                'instrumentation changes stack/timing/refcounts/globals',
                                'unobserved paths and concurrent type mutation not proved']}


class Loader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, filename, recorder, relative):
        super().__init__(fullname, filename)
        self.recorder, self.relative = recorder, relative

    def exec_module(self, module):
        if self.recorder.readers:
            with self.recorder.readers.muted():
                code, helper = self.recorder.compile(self.get_data(self.path), self.relative, filename=self.path)
        else:
            code, helper = self.recorder.compile(self.get_data(self.path), self.relative, filename=self.path)
        module.__dict__[helper] = self.recorder.compare
        exec(code, module.__dict__)


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root, recorder, prefixes=('',)):
        self.root, self.recorder, self.prefixes = Path(root).resolve(), recorder, prefixes

    def record_preexisting(self):
        # pytest_configure may follow conftest imports. Cached functions from
        # these modules escaped before instrumentation, even after a reload.
        for name, module in tuple(sys.modules.items()):
            if type(module) is not types.ModuleType:
                continue
            filename = vars(module).get('__file__')
            if not isinstance(filename, str):
                continue
            path = Path(filename).resolve()
            if path.is_relative_to(self.root):
                relative = path.relative_to(self.root).as_posix()
                if any(relative.startswith(prefix) for prefix in self.prefixes):
                    self.recorder.preexisting_modules[name] = relative

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            filename = Path(spec.origin).resolve()
            if filename.is_relative_to(self.root):
                relative = filename.relative_to(self.root).as_posix()
                if any(relative.startswith(prefix) for prefix in self.prefixes):
                    spec.loader = Loader(fullname, spec.origin, self.recorder, relative)
        return spec

    @contextmanager
    def installed(self):
        self.record_preexisting()
        sys.meta_path.insert(0, self)
        try:
            yield self
        finally:
            sys.meta_path.remove(self)


def single_operator_edit(path, before, after, changed_files=1):
    """Exactly one operator replacement; no chain or other AST differences."""
    if changed_files != 1:
        return None
    old = compile(before.encode() if isinstance(before, str) else before, path, 'exec', ast.PyCF_ONLY_AST)
    new = compile(after.encode() if isinstance(after, str) else after, path, 'exec', ast.PyCF_ONLY_AST)
    old_comparisons = [node for node in ast.walk(old) if isinstance(node, ast.Compare)]
    new_comparisons = [node for node in ast.walk(new) if isinstance(node, ast.Compare)]
    if len(old_comparisons) != len(new_comparisons):
        return None
    edits = [(left, right) for left, right in zip(old_comparisons, new_comparisons)
             if [type(op) for op in left.ops] != [type(op) for op in right.ops]]
    if len(edits) != 1:
        return None
    comparison, changed_comparison = edits[0]
    if len(comparison.ops) != 1 or len(changed_comparison.ops) != 1:
        return None
    left_op, right_op = comparison.ops[0], changed_comparison.ops[0]
    pair = {type(left_op).__name__, type(right_op).__name__}
    if pair not in ({'Is', 'Eq'}, {'IsNot', 'NotEq'}):
        return None
    original_op = comparison.ops[0]
    comparison.ops[0] = type(right_op)()
    same = ast.dump(old, include_attributes=False) == ast.dump(new, include_attributes=False)
    comparison.ops[0] = original_op
    if not same:
        return None
    return {'site': site_key(path, comparison), 'old': type(left_op).__name__, 'new': type(right_op).__name__}


def observed_certificate(mapping, path, before, after, changed_files=1):
    """Conditional algebraic certificate, NEVER an unconditional skip decision."""
    if path in mapping.get('ineligible_files', []):
        return {'equivalent_for_observed_types': False, 'reason': 'module imported before instrumentation'}
    edit = single_operator_edit(path, before, after, changed_files)
    if edit is None:
        return {'equivalent_for_observed_types': False, 'reason': 'not one exact supported operator edit'}
    site = mapping['sites'].get(edit['site'])
    raw = before.encode() if isinstance(before, str) else before
    if not site or site['source_sha256'] != hashlib.sha256(raw).hexdigest() or site['operator'] != edit['old']:
        return {'equivalent_for_observed_types': False, 'reason': 'baseline site/hash mismatch'}
    observations = site['observations']
    if not observations:
        return {'equivalent_for_observed_types': False, 'reason': 'site unobserved'}
    require_ne = edit['old'] in ('IsNot', 'NotEq')
    for pair in observations:
        left, right = pair['left'], pair['right']
        identity = all(t['supported_metaclass'] and t['eq'] in ('object_identity', 'none_identity') and (not require_ne or t['ne'] in ('object_identity', 'none_identity')) for t in (left, right))
        none_case = left['none_type'] or right['none_type']
        enum_case = pair['same_type'] and left['standard_enum_type'] and right['standard_enum_type']
        backed_enum_case = pair['same_type'] and all(
            t.get('standard_enum_type') and t.get('supported_metaclass') and
            t.get('backed_enum_kind') in ('str', 'int') and t.get('canonical_member') and t.get('builtin_values_bijective')
            for t in (left, right)) and left.get('backed_enum_kind') == right.get('backed_enum_kind')
        if not (identity and (none_case or enum_case)) and not backed_enum_case:
            return {'equivalent_for_observed_types': False, 'reason': 'unproved operand type pair', 'site': edit['site']}
    dependencies = mapping.get('read_dependencies', {})
    source_readers = dependencies.get('source', {}).get(path, [])
    if dependencies.get('code_object_granularity'):
        if not site.get('execution_codes'):
            return {'equivalent_for_observed_types': False, 'reason': 'executing code object unobserved'}
        # Ancestor acquisition already records all co_consts descendants. Thus
        # matching the executing code key retains both direct and outer reads.
        code_readers = sorted({context for key in site['execution_codes'] for context in dependencies.get('code_objects', {}).get(key, {}).get('contexts', [])})
    else:
        code_readers = dependencies.get('code', {}).get(path, [])
    return {'equivalent_for_observed_types': True, 'unconditional': False, 'narrowing_authorized': False, 'site': edit['site'],
            'must_retain_observed_source_readers': source_readers,
            'must_retain_observed_code_readers': code_readers,
            'collection_escape_requires_all_tests': '' in source_readers or '' in code_readers,
            'contexts': sorted({context for pair in observations for context in pair['contexts']}),
            'requirements': ['complete deterministic relevant executions', 'unchanged effective type slots',
                             'retain all source/code-object/bytecode readers and unobserved contexts', 'instrumentation transparency verified separately']}


def qualify_patch(mapping, source_root, patch):
    """Developer-only whole-patch adapter; returns no selected/skipped tests.

    Reject unsupported metadata, multiple files, other token edits (including
    comments), chains and any AST change except the exact comparison operator.
    """
    rejected = {'equivalent_for_observed_types': False, 'reason': 'unsupported whole patch'}
    if any(line.startswith(('rename ', 'copy ', 'old mode ', 'new mode ', 'new file ', 'deleted file ', 'GIT binary patch')) for line in patch.splitlines()):
        return rejected
    spec = importlib.util.spec_from_file_location('_comparison_patch_parser', Path(__file__).resolve().parents[1] / 'patches.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        changes = module.parse_patch(patch)
        if len(changes) != 1:
            return rejected
        change = changes[0]
        path = change['path']
        root = Path(source_root).resolve()
        filename = (root / path).resolve()
        if not filename.is_relative_to(root) or filename.suffix != '.py':
            return rejected
        before = filename.read_text()
        after, _ = module.apply_file(before, change)
        edit = single_operator_edit(path, before, after)
        if not edit:
            return rejected
        def tokens(text):
            return [(token.type, token.string) for token in tokenize.generate_tokens(io.StringIO(text).readline)]
        old_tokens, new_tokens = tokens(before), tokens(after)
        differences = [(old_tokens[a:b], new_tokens[c:d]) for tag, a, b, c, d in difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False).get_opcodes() if tag != 'equal']
        names = {'Is': ['is'], 'IsNot': ['is', 'not'], 'Eq': ['=='], 'NotEq': ['!=']}
        if len(differences) != 1 or [[word for _, word in side] for side in differences[0]] != [names[edit['old']], names[edit['new']]]:
            return rejected
        # Width changes can alter unrelated same-line code object's co_positions.
        # Require the entire text outside the one equal-width operator unchanged.
        if {edit['old'], edit['new']} != {'Is', 'Eq'}:
            return rejected
        text_edits = [(before[a:b], after[c:d]) for tag, a, b, c, d in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes() if tag != 'equal']
        if text_edits != [(names[edit['old']][0], names[edit['new']][0])]:
            return rejected
        return dict(observed_certificate(mapping, path, before, after), path=path)
    except (ValueError, SyntaxError, OSError, tokenize.TokenError):
        return rejected


# Optional isolated subprocess pilot, with no baseline test result lookup table.
if os.environ.get('COMPARISON_SOURCE'):
    import pytest
    _context = ''
    _recorder = Recorder(lambda: _context, root=os.environ['COMPARISON_SOURCE'])
    _instrument = os.environ.get('COMPARISON_INSTRUMENT', '1') == '1'
    _finder = Finder(os.environ['COMPARISON_SOURCE'], _recorder, ('sqlglot/',))
    _selected = set(json.loads(Path(os.environ['COMPARISON_NODES_FILE']).read_text()) if os.environ.get('COMPARISON_NODES_FILE') else json.loads(os.environ['COMPARISON_NODES']))
    _reports, _details = {}, {}
    _collected, _finished, _collection_errors = set(), set(), []
    _restore_spawn = None
    def pytest_configure(config):
        global _restore_spawn
        if _instrument:
            _finder.record_preexisting()
            _recorder.readers.start()
            sys.meta_path.insert(0, _finder)
            if os.environ.get('COMPARISON_SPAWN_OUTPUT'):
                from comparison_spawn import install_parent
                _restore_spawn = install_parent(lambda: _context, os.environ['COMPARISON_SOURCE'], os.environ['COMPARISON_SPAWN_OUTPUT'])
    def pytest_collectreport(report):
        if report.failed:
            _collection_errors.append({'nodeid': report.nodeid, 'error': str(report.longrepr)})
    def pytest_collection_modifyitems(session, config, items):
        items[:] = [item for item in items if item.nodeid in _selected]
        _collected.update(item.nodeid for item in items)
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(item, nextitem):
        global _context
        _context = item.nodeid
        try:
            yield
        finally:
            _finished.add(item.nodeid)
            _context = ''
    def pytest_runtest_logreport(report):
        detail = _details.setdefault(report.nodeid, {'phases': {}, 'subtests': {}, 'failures': []})
        subtest = type(report).__name__ == 'SubTestReport' or hasattr(report, 'context')
        if subtest:
            detail['subtests'][report.outcome] = detail['subtests'].get(report.outcome, 0) + 1
        else:
            detail['phases'][report.when] = report.outcome
        if report.failed:
            _reports[report.nodeid] = 'failed'
            detail['failures'].append({'when': report.when, 'subtest': subtest, 'error': str(report.longrepr)})
        elif report.skipped and _reports.get(report.nodeid) != 'failed':
            _reports[report.nodeid] = 'skipped'
        elif not subtest and report.when == 'call' and report.nodeid not in _reports:
            _reports[report.nodeid] = report.outcome
    def pytest_sessionfinish(session, exitstatus):
        if _restore_spawn:
            _restore_spawn()
        if _instrument:
            sys.meta_path.remove(_finder)
            _recorder.readers.stop()
        Path(os.environ['COMPARISON_OUTPUT']).write_text(json.dumps(_recorder.snapshot(), separators=(',', ':'), sort_keys=True))
        report = {'selected': sorted(_selected), 'collected': sorted(_collected), 'finished': sorted(_finished),
                  'missing': sorted(_selected - _finished), 'outcomes': {node: _reports.get(node, 'missing') for node in sorted(_selected)},
                  'details': _details, 'collection_errors': _collection_errors, 'exitstatus': int(exitstatus),
                  'configuration': {'cpu_count': os.cpu_count(), 'process_cpu_count': os.process_cpu_count(),
                                    'PYTHON_CPU_COUNT': os.environ.get('PYTHON_CPU_COUNT'), 'python': sys.version,
                                    'instrumented': _instrument, 'spawn_capture': bool(_restore_spawn)}}
        Path(os.environ['COMPARISON_OUTPUT'] + '.pilot.json').write_text(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--node', action='append', default=[])
    parser.add_argument('--measure-overhead', action='store_true')
    parser.add_argument('--all-tests', action='store_true')
    parser.add_argument('--spawn-output', type=Path)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--cpu-count', type=int, default=2)
    args = parser.parse_args()
    source = args.source.resolve()
    if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != PIN or subprocess.check_output(['git', '-C', str(source), 'diff', '--name-only', 'HEAD'], text=True):
        raise SystemExit('Pilot requires clean pinned baseline')
    tests = json.loads(args.catalog.read_text())['tests']
    nodes = [test['id'] for test in tests] if args.all_tests else args.node
    if not nodes:
        files = set()
        for test in sorted(tests, key=lambda entry: (entry['seconds'], entry['id'])):
            if test['file'] not in files:
                files.add(test['file']); nodes.append(test['id'])
            if len(nodes) == 3:
                break
    if not set(nodes) <= {test['id'] for test in tests}:
        raise SystemExit('Unknown catalog node')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith(('BLAST_', 'COMPARISON_')) and key != 'PYTEST_ADDOPTS'}
    nodes_path = Path(str(args.output.resolve()) + '.nodes.json')
    nodes_path.write_text(json.dumps(nodes))
    if args.spawn_output:
        args.spawn_output.mkdir(parents=True, exist_ok=True)
        if any(args.spawn_output.iterdir()):
            raise SystemExit('Spawn output must be empty: receipts cannot mix runs')
        env['COMPARISON_SPAWN_OUTPUT'] = str(args.spawn_output.resolve())
    env.update(COMPARISON_SOURCE=str(source), COMPARISON_OUTPUT=str(args.output.resolve()), COMPARISON_NODES_FILE=str(nodes_path),
               PYTHON_CPU_COUNT=str(args.cpu_count), OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1',
               PYTHONPATH=str(Path(__file__).parent.resolve()) + os.pathsep + str(source),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1',
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''))
    command = [sys.executable, '-X', f'cpu_count={args.cpu_count}', '-m', 'pytest', 'tests', '-p', 'comparison_types', '-q', '-p', 'no:cacheprovider']
    timing = {'cpu_count': args.cpu_count, 'timeout_seconds': args.timeout, 'blas_threads': 1}
    def run(environment, label):
        process = subprocess.Popen(command, cwd=source, env=environment, start_new_session=True)
        print(f'{label}: PID={process.pid}, nodes={len(nodes)}, timeout={args.timeout}', flush=True)
        try:
            return process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return 124
    if args.measure_overhead:
        plain_env = dict(env, COMPARISON_INSTRUMENT='0', COMPARISON_OUTPUT=str(args.output.resolve()) + '.plain')
        start = time.monotonic()
        plain = run(plain_env, 'plain control')
        timing['plain_seconds'] = time.monotonic() - start
        timing['plain_exitcode'] = plain
        Path(str(args.output) + '.timing.json').write_text(json.dumps(timing, indent=2))
        if plain != 0:
            raise SystemExit('Plain control failed; inspect outcomes before instrumented run')
    start = time.monotonic()
    result = run(env, 'instrumented trace')
    timing['instrumented_seconds'] = time.monotonic() - start
    timing['instrumented_exitcode'] = result
    if args.spawn_output:
        from comparison_spawn import audit_receipts
        Path(str(args.output) + '.spawn-audit.json').write_text(json.dumps(audit_receipts(args.spawn_output), indent=2))
    Path(str(args.output) + '.timing.json').write_text(json.dumps(timing, indent=2))
    raise SystemExit(result)

if __name__ == '__main__':
    main()
