"""Extract private review candidates without publishing corpus identifiers."""
import argparse
import json
from pathlib import Path
import re
from . import native
from . import neutral
from . import pdf_handler
from . import solidworks

CONTACT = re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\bhttps?://[^\s<>]+', re.I)

def scan(root):
    for p in sorted(root.rglob('*')):
        if not p.is_file(): continue
        report = {'path':str(p.relative_to(root)), 'bytes':p.stat().st_size}
        try:
            if p.stat().st_size > 20_000_000:
                report.update(status='deferred_large_file', candidates=[])
            elif solidworks.MARKER in p.read_bytes()[:64]:
                streams, issues = solidworks.read_streams(p.read_bytes())
                report.update(status='solidworks_metadata',issues=issues,candidates=[s.payload.decode('utf-8') for s in streams if s.name.startswith('docProps/') and s.name.endswith('.xml')])
            elif p.suffix.lower() in neutral.SUPPORTED:
                report.update(status='metadata_extracted',candidates=neutral.extract(p.read_bytes(),p.suffix.lower()))
            elif p.suffix.lower() == '.pdf':
                report.update(status='text_extracted',candidates=pdf_handler.extract(p.read_bytes()))
            else:
                details=native.inspect_native(p)
                report.update(status=details['format'],native=details,candidates=[x['text'] for x in details['properties']])
            report['contacts']=sorted(set(CONTACT.findall('\n'.join(report['candidates']))))
        except Exception as e:
            report.update(status='error',error=type(e).__name__+': '+str(e))
        yield report

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path);p.add_argument('--private-report',type=Path,required=True)
    a=p.parse_args();a.private_report.parent.mkdir(parents=True,exist_ok=True)
    with a.private_report.open('w') as out:
        for row in scan(a.input):out.write(json.dumps(row)+'\n')
    a.private_report.chmod(0o600)

if __name__=='__main__':main()
