"""Explicitly opt-in experimental comparison proposals; no production approval.

No outcomes/answers are loaded. An intact capture is still conditional evidence
about observed executions, not proof of reader completeness or order stability.
"""
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tokenize
import zlib
from comparison_audit import capture_issues
from comparison_edit import exact_operator_edit
from comparison_proof import observed_certificate
from patches import parse_patch, apply_file

MAX_COMPRESSED = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


def _unique(pairs):
    result = {}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _json(raw):
    def invalid(value): raise ValueError('nonstandard JSON constant')
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=invalid)


def qualify(mapping, source, patch):
    rejected = {'equivalent_for_observed_types': False, 'reason': 'unsupported whole patch'}
    if any(line.startswith(('rename ', 'copy ', 'old mode ', 'new mode ', 'new file ', 'deleted file ', 'GIT binary patch')) for line in patch.splitlines()):
        return rejected
    try:
        changes = parse_patch(patch)
        if len(changes) != 1: return rejected
        change = changes[0]; path = change['path']; root = Path(source).resolve(); filename = (root/path).resolve()
        if not filename.is_relative_to(root) or filename.suffix != '.py': return rejected
        before = filename.read_text(); after,_ = apply_file(before,change)
        if exact_operator_edit(path,before,after) is None: return rejected
        return dict(observed_certificate(mapping,path,before,after),path=path)
    except (ValueError, SyntaxError, OSError, tokenize.TokenError):
        return rejected


class ExperimentalComparisons:
    def __init__(self, tests, production_map, source, evidence=None, manifest=None, *, enabled=False):
        self.tests=tests; self.production=production_map; self.source=Path(source)
        self.by_id={t['id']:t['number'] for t in tests}; self.all=set(self.by_id.values())
        self.enabled=enabled; self.mapping=None; self.issue='experimental comparison disabled'; self.last_detail={}
        self.unstable=set(); self.invalid_protection=False
        values=production_map.get('baseline_unstable_tests',[])
        if type(values) is not list or any(type(n) is not str or n not in self.by_id for n in values):
            self.invalid_protection=True
        else:self.unstable={self.by_id[n] for n in values}
        if not enabled:return
        try:
            meta=_json(Path(manifest).read_bytes())
            if meta.get('format') != 1 or meta.get('experimental_only') is not True or meta.get('production_authorized') is not False:
                raise ValueError('not an experimental manifest')
            if meta.get('python_version') != list(sys.version_info[:2]): raise ValueError('interpreter mismatch')
            directory=Path(__file__).resolve().parent
            expected={'comparison_proof.py','comparison_audit.py','comparison_edit.py','patches.py','comparison_selection.py','selector.py'}
            if set(meta.get('runtime_hashes',{})) != expected: raise ValueError('missing runtime hash binding')
            for name,digest in meta['runtime_hashes'].items():
                if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=digest:raise ValueError('runtime hash mismatch')
            path=Path(evidence)
            if path.stat().st_size>MAX_COMPRESSED:raise ValueError('compressed evidence too large')
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=meta['compressed_sha256']:raise ValueError('artifact hash mismatch')
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream: expanded=stream.read(MAX_EXPANDED+1)
            if len(expanded)>MAX_EXPANDED:raise ValueError('expanded evidence too large')
            if hashlib.sha256(expanded).hexdigest()!=meta['expanded_sha256']:raise ValueError('expanded hash mismatch')
            mapping=_json(expanded)
            issues=capture_issues(mapping,tests)
            versions=[mapping.get('python_version'),mapping.get('capture_report',{}).get('python_version')]
            current='.'.join(map(str,sys.version_info[:2]))
            if any(v not in (current,list(sys.version_info[:2])) for v in versions):issues.append('capture interpreter mismatch')
            if issues:raise ValueError('; '.join(issues))
            self.mapping=mapping;self.issue=None
        except (OSError,ValueError,TypeError,KeyError,AttributeError,EOFError,RecursionError,zlib.error) as exc:
            self.issue=str(exc)

    def retain(self, original, reason):
        self.last_detail={'decision':'retain_base_selection','reason':reason}
        return self.all if self.invalid_protection else set(original)|self.unstable

    def propose(self, patch, original, base_status):
        original=set(original)
        if self.invalid_protection:return self.retain(original,'unresolved unstable baseline metadata')
        if not self.enabled or self.mapping is None:return self.retain(original,self.issue)
        if base_status != {'complete':True,'reason':'dependency_selection'}:
            return self.retain(original,'base fallback or unknown selection provenance')
        try:
            mapping=self.mapping; cert=qualify(mapping,self.source,patch)
            if cert.get('equivalent_for_observed_types') is not True:return self.retain(original,cert.get('reason','unproved edit'))
            path=cert['path']
            if path in mapping['ineligible_files'] or path in mapping['preexisting_modules'].values():return self.retain(original,'preexisting module')
            if cert.get('collection_escape_requires_all_tests') is not False:return self.retain(original,'collection escape')
            observed=set(cert['contexts']);readers=set(cert['must_retain_observed_source_readers'])|set(cert['must_retain_observed_code_readers'])
            executing={c for record in mapping['sites'][cert['site']]['execution_codes'].values() for c in record['contexts']}
            if (observed|readers|executing)-(set(self.by_id)|{''}):return self.retain(original,'unknown comparison context')
            if '' in readers:return self.retain(original,'global reader')
            info=self.production.get('files',{}).get(path)
            if not isinstance(info,dict) or info.get('collection_read'):return self.retain(original,'unknown or global production reader')
            ids=self.production.get('test_ids',[])
            if type(ids) is not list or len(set(ids))!=len(ids) or any(n not in self.by_id for n in ids):return self.retain(original,'unknown production catalog')
            # Unresolved line/read indices must not be silently omitted by the
            # base selector's translation helper.
            indices=list(info.get('read_tests',[]))
            for entries in info.get('lines',{}).values():indices.extend(entries)
            if any(type(i) is not int or not 0<=i<len(ids) for i in indices):return self.retain(original,'unknown production index')
            protected={self.by_id[n] for n in readers}|self.unstable
            protected.update(self.by_id[ids[i]] for i in info.get('read_tests',[]))
            missing=self.production.get('missing_tests',[])
            outcomes=self.production.get('baseline_outcomes',{})
            if type(missing) is not list or not isinstance(outcomes,dict) or any(n not in self.by_id for n in set(missing)|set(outcomes)):return self.retain(original,'unknown baseline outcome context')
            uncertain=(set(self.by_id)-set(ids))|set(missing)|{n for n,v in outcomes.items() if v!='passed'}
            protected.update(self.by_id[n] for n in uncertain)
            exact={self.by_id[n] for n in observed&executing if n in self.by_id}
            selected=(original-exact)|protected
            self.last_detail={'decision':'conditional_experimental_proposal','site':cert['site'],'removed_count':len(original-selected),'production_authorized':False}
            return selected
        except (ValueError,TypeError,KeyError,AttributeError,OSError) as exc:
            return self.retain(original,'invalid dependency metadata: '+type(exc).__name__)
