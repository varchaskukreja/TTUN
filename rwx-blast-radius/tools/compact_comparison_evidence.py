#!/usr/bin/env python3
"""Lossless research serialization, not a runtime authorization or selector.

Intern repeated JSON strings/containers into an acyclic node table. Decoding
recreates containers independently: the expanded mapping has no new aliases.
"""
import argparse
import gzip
import hashlib
import json
import time
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def pack(value):
    nodes, interned = [], {}
    def visit(x):
        if isinstance(x, dict):
            if any(type(k) is not str for k in x): raise ValueError('non-string JSON key')
            key = ('d', tuple((visit(k), visit(v)) for k,v in sorted(x.items())))
        elif isinstance(x, list): key = ('l', tuple(visit(v) for v in x))
        elif type(x) is str: key = ('s', x)
        elif x is None: key = ('n',)
        elif type(x) is bool: key = ('b', x)
        elif type(x) is int: key = ('i', x)
        elif type(x) is float:
            if not (-float('inf') < x < float('inf')): raise ValueError('nonfinite JSON')
            key = ('f', repr(x))
        else: raise ValueError('unsupported JSON value')
        if key not in interned:
            interned[key] = len(nodes)
            nodes.append(key)
        return interned[key]
    root = visit(value)
    return {'format': 'comparison-json-dag-v1', 'root': root, 'nodes': nodes,
            'expanded_sha256': hashlib.sha256(canonical(value)).hexdigest(),
            'narrowing_authorized': False}


def unpack(artifact):
    if artifact.get('format') != 'comparison-json-dag-v1' or artifact.get('narrowing_authorized') is not False:
        raise ValueError('invalid research format')
    nodes = artifact['nodes']
    if type(nodes) is not list or type(artifact['root']) is not int or not 0 <= artifact['root'] < len(nodes):
        raise ValueError('invalid nodes/root')
    def read(index, parent):
        if type(index) is not int or not 0 <= index < parent: raise ValueError('non-acyclic reference')
        node = nodes[index]
        if not isinstance(node, (list,tuple)) or not node: raise ValueError('invalid node')
        tag = node[0]
        if tag == 'n' and len(node) == 1: return None
        if len(node) != 2: raise ValueError('invalid node arity')
        value = node[1]
        if tag == 's' and type(value) is str: return value
        if tag == 'b' and type(value) is bool: return value
        if tag == 'i' and type(value) is int: return value
        if tag == 'f' and type(value) is str:
            result = float(value)
            if not (-float('inf') < result < float('inf')): raise ValueError('nonfinite JSON')
            return result
        if tag == 'l' and isinstance(value, (list,tuple)): return [read(v,index) for v in value]
        if tag == 'd' and isinstance(value, (list,tuple)):
            result = {}
            for pair in value:
                if not isinstance(pair,(list,tuple)) or len(pair)!=2: raise ValueError('invalid dict pair')
                k,v=pair; key=read(k,index)
                if type(key) is not str or key in result: raise ValueError('invalid/duplicate dict key')
                result[key]=read(v,index)
            return result
        raise ValueError('invalid node type')
    result = read(artifact['root'], len(nodes))
    if hashlib.sha256(canonical(result)).hexdigest() != artifact['expanded_sha256']:
        raise ValueError('expanded hash mismatch')
    return result


def load(path):
    return unpack(json.loads(gzip.decompress(Path(path).read_bytes())))


def load_plain(path, expected_expanded_sha256):
    """Preferred simple representation; expected digest comes from pinned manifest."""
    raw = gzip.decompress(Path(path).read_bytes())
    if hashlib.sha256(raw).hexdigest() != expected_expanded_sha256:
        raise ValueError('expanded hash mismatch')
    return json.loads(raw)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True);p.add_argument('--output',required=True);p.add_argument('--report',required=True);p.add_argument('--encoding',choices=('plain','dag'),default='plain')
    a=p.parse_args();raw=Path(a.input).read_bytes();original=json.loads(raw)
    start=time.process_time();artifact=pack(original);packed_cpu=time.process_time()-start
    encoded=canonical(artifact);compressed=gzip.compress(raw if a.encoding=='plain' else encoded,compresslevel=9,mtime=0);Path(a.output).write_bytes(compressed)
    start=time.process_time();restored=load_plain(a.output,hashlib.sha256(raw).hexdigest()) if a.encoding=='plain' else load(a.output);load_cpu=time.process_time()-start
    if restored != original: raise ValueError('roundtrip mismatch')
    report={'kind':'lossless_comparison_evidence_research','encoding':a.encoding,'narrowing_authorized':False,'input_sha256':hashlib.sha256(raw).hexdigest(),'artifact_sha256':hashlib.sha256(compressed).hexdigest(),'expanded_sha256':hashlib.sha256(raw).hexdigest() if a.encoding=='plain' else artifact['expanded_sha256'],'canonical_sha256':artifact['expanded_sha256'],'input_bytes':len(raw),'plain_gzip_bytes':len(gzip.compress(raw,mtime=0)),'dag_json_bytes':len(encoded),'artifact_bytes':len(compressed),'nodes':len(artifact['nodes']),'sites':len(original['sites']),'lossless_equality':True,'pack_cpu_seconds':packed_cpu,'load_verify_cpu_seconds':load_cpu}
    Path(a.report).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))

if __name__=='__main__':main()
