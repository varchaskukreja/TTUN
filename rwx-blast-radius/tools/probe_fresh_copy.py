#!/usr/bin/env python3
"""Standalone baseline helper/ownership probes. Never patches baseline files.

No mutation-answer lookup, selector, coverage capture, or production narrowing.
All monkeypatches below are synthetic counterexamples in this disposable process.
"""
import argparse
import ast
import copy as copy_module
from collections import Counter
import gc
import hashlib
import json
from pathlib import Path
import subprocess
import sys

PIN='eb11aec4be8cb2122afe713a4816662c3f0fb2fa'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=args.source.resolve()
    assert subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()==PIN
    assert not subprocess.check_output(['git','-C',str(root),'diff','--name-only','HEAD'],text=True).strip()
    sys.path.insert(0,str(root))
    from sqlglot import exp
    from sqlglot.expressions import builders
    rows=[]
    def run(name, function):
        values={str(copy).lower():function(copy) for copy in (False,True)}
        rows.append({'probe':name,'results':values,'observably_different':values['false']!=values['true']})
    def primitive(copy):
        value=exp.alias_(exp.table_('fresh_copy_probe'),alias='a',copy=copy)
        return {'sql':value.sql(),'dump':value.dump(),'root_parent_none':value.parent is None,
                'child_parent_is_root':value.this.parent is value,'arg_keys':list(value.args)}
    run('exact_builtin_primitive_structural_control',primitive)
    def metadata(copy):
        calls=[]
        class Metadata:
            def __init__(self,generation=0):self.generation=generation
            def __deepcopy__(self,memo):
                calls.append('deepcopy')
                return Metadata(self.generation+1)
        name=exp.to_identifier('fresh_copy_probe');name.meta['observer']=Metadata()
        value=exp.alias_(exp.table_(name),alias='a',copy=copy)
        count=len(calls)
        return {'sql':value.sql(),'deepcopy_calls_before_sql':count,'generation':value.this.meta['observer'].generation}
    run('fresh_root_with_external_identifier_metadata',metadata)
    def shared_graph(copy):
        def factory():
            child=exp.Literal.number(1)
            return exp.Tuple(expressions=[child,child])
        value=exp.alias_(factory(),alias='a',copy=copy)
        children=value.this.expressions
        return {'sql':value.sql(),'same_child_identity':children[0] is children[1],
                'child_indices':[child.index for child in children]}
    run('fresh_owned_graph_with_internal_sharing',shared_graph)
    def constructor(copy):
        calls=[]
        class TrackingTable(exp.Table):
            def __init__(self,**kwargs):
                super().__init__(**kwargs);calls.append(self)
        original=builders.Table;builders.Table=TrackingTable
        try:
            value=exp.alias_(exp.table_('fresh_copy_probe'),alias='a',copy=copy)
            return {'constructor_calls':len(calls),'first_constructed_is_result':calls[0] is value}
        finally:builders.Table=original
    run('rebound_constructor_side_effect_and_escape',constructor)
    def profile_escape(copy):
        escaped=[]
        constructor_code=exp.Expr.__init__.__code__
        def observer(frame,event,arg):
            if event=='return' and frame.f_code is constructor_code:
                value=frame.f_locals.get('self')
                if type(value) is exp.Table:escaped.append(value)
        old=sys.getprofile();sys.setprofile(observer)
        try:value=exp.alias_(exp.table_('fresh_copy_probe'),alias='a',copy=copy)
        finally:sys.setprofile(old)
        return {'sql':value.sql(),'escaped_tables':len(escaped),'first_constructed_is_result':escaped[0] is value}
    run('unchanged_builtin_constructor_profile_escape',profile_escape)
    def copy_hook(copy):
        calls=[];original=exp.Table.copy
        def observer(self):calls.append('copy');return original(self)
        exp.Table.copy=observer
        try:
            value=exp.alias_(exp.table_('fresh_copy_probe'),alias='a',copy=copy)
            count=len(calls)
            return {'sql':value.sql(),'copy_hook_calls_before_sql':count}
        finally:del exp.Table.copy
    run('changed_copy_method_binding',copy_hook)
    def dispatch_hook(copy):
        calls=[];sentinel=object();old=copy_module._deepcopy_dispatch.get(exp.Table,sentinel)
        def observer(value,memo):calls.append('dispatch');return value
        copy_module._deepcopy_dispatch[exp.Table]=observer
        try:
            value=exp.alias_(exp.table_('fresh_copy_probe'),alias='a',copy=copy)
            return {'dispatch_calls':len(calls),'result_type':type(value).__name__}
        finally:
            if old is sentinel:del copy_module._deepcopy_dispatch[exp.Table]
            else:copy_module._deepcopy_dispatch[exp.Table]=old
    run('copy_dispatch_registry_changed_without_method_rebinding',dispatch_hook)
    def extra_state(copy):
        def factory():
            value=exp.table_('fresh_copy_probe');value.extra_observable='present';return value
        value=exp.alias_(factory(),alias='a',copy=copy)
        return {'sql':value.sql(),'has_extra_state':hasattr(value,'extra_observable')}
    run('fresh_root_extra_instance_state_not_copied',extra_state)
    def gc_observer(copy):
        gc.collect();enabled=gc.isenabled();gc.disable()
        try:
            value=exp.alias_(exp.table_('fresh_copy_gc_probe'),alias='a',copy=copy)
            count=sum(type(item) is exp.Table and item.name=='fresh_copy_gc_probe' for item in gc.get_objects())
            result={'sql':value.sql(),'gc_visible_tables':count}
            del value
            return result
        finally:
            gc.collect()
            if enabled:gc.enable()
    run('extra_builtin_cyclic_allocation_visible_to_gc',gc_observer)
    assert rows[0]['observably_different'] is False
    assert all(row['observably_different'] for row in rows[1:])
    files=['sqlglot/expressions/core.py','sqlglot/expressions/builders.py']
    candidates=[]
    for path in sorted((root/'sqlglot').rglob('*.py')):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and node.args and isinstance(node.args[0],ast.Call) and any(keyword.arg=='copy' and isinstance(keyword.value,ast.Constant) and type(keyword.value.value) is bool for keyword in node.keywords):
                candidates.append({'path':path.relative_to(root).as_posix(),'line':node.lineno,'outer':ast.unparse(node.func),'inner':ast.unparse(node.args[0].func)})
    report={'format':1,'kind':'fresh_inline_ast_copy_feasibility','baseline_revision':PIN,'python':sys.version,
            'source_sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files},
            'probes':rows,'all_assertions_passed':True,'syntactic_inline_copy_sites':candidates,'syntactic_sites_are_not_proofs':True,'source_files_modified':False,'runtime_narrowing_authorized':False,
            'conclusion':'Fresh outer construction alone does not imply copy equivalence; even exact builtin primitive construction is observable through profiling or GC.'}
    args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))

if __name__=='__main__':main()
