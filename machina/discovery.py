"""Build a private identity candidate ledger from semantic metadata and provenance.

Candidates are evidence for review, not automatically approved replacement rules.
Material names, geometry labels and generic product titles are never auto-promoted.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from . import neutral, solidworks
from .inventory import rows

EMAIL = re.compile(r'(?<![\w.+-])[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')
URL = re.compile(r'\bhttps?://([^\s/<>"\']+)', re.I)
USER_PATH = re.compile(r'(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|/(?:Users|home)/)([^/\\\r\n]+)',re.I)
NON_USERS={'public','default','default user','all users','shared','administrator','admin','root'}
EMPTY={'','none','unknown','unspecified','not specified','n/a','null'}


def candidate(value, kind, source, field, confidence='review'):
    value=value.strip()
    if value.casefold() in EMPTY or len(value)>512 or not any(c.isalpha() for c in value):return None
    return {'identity':value,'kind':kind,'source':source,'field':field,'confidence':confidence}


def contacts(text, source):
    for match in EMAIL.finditer(text):
        yield candidate(match.group(),'email',source,'email_pattern','strong')
    for match in URL.finditer(text):
        # Port and punctuation are syntax, not part of a hostname identity.
        host=match[1].rstrip('.,;:)').split(':')[0]
        yield candidate(host,'domain',source,'url_host')
    for match in USER_PATH.finditer(text):
        if match[1].casefold() not in NON_USERS:
            yield candidate(match[1],'account',source,'user_directory','strong')


def step_metadata(data, source):
    text,_=neutral._decode(data)
    tokens=re.finditer(r"/\*.*?\*/|'(?:[^']|'')*'|[A-Za-z_][A-Za-z_0-9]*|[^\s]",text,re.S)
    pending=False;active=False;depth=arg=0
    for token in tokens:
        v=token.group()
        if v.startswith('/*'):continue
        if active:
            if v=='(':depth+=1
            elif v==')':
                depth-=1
                if depth==0:active=False
            elif v==',' and depth==1:arg+=1
            elif v.startswith("'"):
                value=v[1:-1].replace("''", "'")
                if arg in {2,3}:
                    yield candidate(value,'person' if arg==2 else 'company',source,'step_author' if arg==2 else 'step_organization','strong')
                if arg==0:yield from contacts(value,source)
        elif pending and v=='(':
            pending=False;active=True;depth=1;arg=0
        else:pending=v.upper()=='FILE_NAME'


def xml_metadata(data, source):
    try:root=ET.fromstring(data)
    except ET.ParseError:return
    roles={'creator':'person','lastmodifiedby':'person','author':'person','company':'company','manager':'person'}
    for element in root.iter():
        tag=element.tag.rsplit('}',1)[-1].lower()
        if tag in roles and element.text:
            yield candidate(element.text,roles[tag],source,'xml_'+tag,'strong')


def provenance(path):
    parts=path.split('/')
    if len(parts)>=3 and parts[:2]==['sources','github']:
        owner,separator,project=parts[2].partition('__')
        if separator:
            yield candidate(owner,'account','/'.join(parts[:3]),'github_owner','strong')
            # Project names can be generic engineering labels, so review them.
            yield candidate(project,'project','/'.join(parts[:3]),'repository_name')
    if len(parts)>=3 and parts[:2]==['sources','web']:
        yield candidate(parts[2],'domain','/'.join(parts[:3]),'source_domain')


def scan_candidates(record):
    source=record['path']
    yield from provenance(source)
    for text in record.get('candidates',[]):yield from contacts(text,source)
    native=record.get('native',{})
    for prop in native.get('properties',[]):
        semantic=prop.get('semantic')
        if semantic:
            kind='company' if semantic in {'company','vendor','manufacturer'} else 'person'
            yield candidate(prop['text'],kind,source,'ole_'+semantic,'strong')
            continue
        stream=prop['stream'][-1]
        pid=prop['property_id']
        if stream=='\x05SummaryInformation' and pid in {4,8}:
            yield candidate(prop['text'],'person',source,'ole_author' if pid==4 else 'ole_last_saved_by','strong')
        if stream=='\x05DocumentSummaryInformation' and pid in {14,15}:
            yield candidate(prop['text'],'company' if pid==15 else 'person',source,'ole_company' if pid==15 else 'ole_manager','strong')
    if record.get('status')=='solidworks_metadata':
        for text in record.get('candidates',[]):yield from xml_metadata(text,source)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scan-report',type=Path)
    p.add_argument('--manifest',type=Path)
    p.add_argument('--input',type=Path,help='Optional same root used by scan.py, for typed STEP header discovery')
    p.add_argument('--private-output',type=Path,required=True)
    a=p.parse_args()
    if not a.scan_report and not a.manifest:p.error('Provide --scan-report or --manifest')
    a.private_output.parent.mkdir(parents=True,exist_ok=True)
    counts=Counter();seen=set()
    with a.private_output.open('x') as out:
        a.private_output.chmod(0o600)
        def emit(entries):
            for c in entries:
                if c is None:continue
                key=(c['identity'].casefold(),c['kind'],c['source'],c['field'])
                if key in seen:continue
                seen.add(key);counts[c['kind']]+=1;out.write(json.dumps(c)+'\n')
        if a.manifest:
            for row in rows(a.manifest):emit(provenance(row['path']))
        if a.scan_report:
            for line in a.scan_report.open():
                record=json.loads(line);emit(scan_candidates(record))
                if a.input:
                    path=(a.input/record['path']).resolve()
                    if not path.is_relative_to(a.input.resolve()):raise ValueError('Unsafe scan path')
                    if path.suffix.lower() in {'.step','.stp'} and path.stat().st_size<256_000_000:
                        emit(step_metadata(path.read_bytes(),record['path']))
    print(json.dumps({'candidate_evidence':sum(counts.values()),'by_kind':counts,'approved_identities':0},indent=2))

if __name__=='__main__':main()
