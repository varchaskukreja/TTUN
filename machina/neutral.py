"""Conservative, span-preserving neutral CAD metadata editing.

The caller supplies a corpus-wide identity-aware replacement function. Extraction
returns candidate strings, not a claim that every returned word is identifying.
Numeric geometry, format keys, units and schema declarations are never candidates.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Callable

SUPPORTED = {'.step', '.stp', '.stl', '.obj', '.svg', '.dxf', '.sat'}
_STRING = re.compile(r"'(?:[^']|'')*'", re.S)


def _step_spans(text):
    # Tokenize once so comments, quoted semicolons and nested argument lists
    # cannot trick statement matching. Span offsets refer to original text.
    tokens = re.finditer(r"/\*.*?\*/|'(?:[^']|'')*'|[A-Za-z_][A-Za-z_0-9]*|[^\s]", text, re.S)
    allowed = {'FILE_NAME': {0, 2, 3, 6}, 'FILE_DESCRIPTION': {0}, 'PRODUCT': {0, 1, 2}}
    active = pending = None
    depth = arg = 0
    for token in tokens:
        value = token.group()
        if value.startswith('/*'):
            continue
        if active:
            if value == '(':
                depth += 1
            elif value == ')':
                depth -= 1
                if depth == 0:
                    active = None
            elif value == ',' and depth == 1:
                arg += 1
            elif value.startswith("'") and arg in allowed[active]:
                yield token.start() + 1, token.end() - 1, value[1:-1].replace("''", "'"), 'step'
        elif pending and value == '(':
            active, pending, depth, arg = pending, None, 1, 0
        else:
            pending = value.upper() if value.upper() in allowed else None


def _sat_spans(text):
    # SAT's second header line stores length-prefixed product and version text.
    # Its third string is a timestamp; remaining records include geometry and
    # undocumented attributes and are deliberately left for specialized handlers.
    lines = text.splitlines(keepends=True)
    if len(lines) < 3 or not re.fullmatch(r'\s*\d+\s+\d+\s+\d+\s+\d+\s*', lines[0]):
        raise ValueError('Not an ASCII SAT header')
    pos = len(lines[0])
    end = pos + len(lines[1])
    for _ in range(2):
        match = re.match(r'\s*(\d+) ', text[pos:end])
        if not match:
            raise ValueError('Invalid SAT length-prefixed header')
        start = pos + match.start(1)
        value_start = pos + match.end()
        value_end = value_start + int(match[1])
        if value_end > end:
            raise ValueError('Truncated SAT header string')
        yield start, value_end, text[value_start:value_end], 'sat'
        pos = value_end


def _line_spans(text, suffix):
    offset = 0
    if suffix == '.dxf':
        lines = text.splitlines(keepends=True)
        if len(lines) % 2:
            raise ValueError('DXF has incomplete group pair')
        entity = variable = ''
        for i in range(0, len(lines), 2):
            code = lines[i].strip()
            if not re.fullmatch(r'\d+', code):
                raise ValueError('Not an ASCII DXF group code')
            value = lines[i + 1].rstrip('\r\n')
            start = offset + len(lines[i])
            if code == '0':
                entity, variable = value.strip().upper(), ''
            if code == '9':
                variable = value.strip().upper()
            if (code == '999' or
                entity in {'TEXT', 'MTEXT', 'ATTRIB', 'ATTDEF'} and code in {'1', '3'} or
                variable == '$LASTSAVEDBY' and code == '1'):
                yield start, start + len(value), value, 'plain'
            offset += len(lines[i]) + len(lines[i + 1])
        return
    pattern = r'^(\s*(?:solid|endsolid)\s+)(.*?)(\r?\n)?$' if suffix == '.stl' else r'^(\s*(?:#|o|g|usemtl|mtllib)\s+)(.*?)(\r?\n)?$'
    for line in text.splitlines(keepends=True):
        match = re.match(pattern, line)
        if match:
            yield offset + match.start(2), offset + match.end(2), match[2], 'plain'
        offset += len(line)


def _svg_spans(text):
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.I):
        raise ValueError('SVG DTD/entity declarations require separate handling')
    ET.fromstring(text)  # Validate before retaining its exact original syntax.
    stack = []
    relevant = {'text', 'tspan', 'textpath', 'title', 'desc', 'metadata'}
    # Treat comments and CDATA as indivisible so angle brackets in them do not
    # corrupt the element stack. Metadata comments are identifying candidates.
    for m in re.finditer(r'<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<[^>]*>|[^<]+', text):
        value = m.group()
        if value.startswith('<!--'):
            yield m.start()+4, m.end()-3, value[4:-3], 'comment'
        elif value.startswith('<![CDATA['):
            if any(s in relevant for s in stack):
                yield m.start()+9, m.end()-3, value[9:-3], 'cdata'
        elif value.startswith('</'):
            if stack:
                stack.pop()
        elif value.startswith('<'):
            if not value.startswith('<?') and not value.endswith('/>'):
                name = re.match(r'<\s*([^\s/>]+)', value)
                if name:
                    stack.append(name[1].split(':')[-1].lower())
        elif any(s in relevant for s in stack):
            yield m.start(), m.end(), html.unescape(value), 'xml'


def _decode(data):
    try:
        return data.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        return data.decode('latin-1'), 'latin-1'


def _spans(text, suffix):
    if suffix in {'.step', '.stp'}:
        if not text.lstrip().upper().startswith('ISO-10303-21;'):
            raise ValueError('Missing STEP exchange header')
        return list(_step_spans(text))
    if suffix == '.svg':
        return list(_svg_spans(text))
    if suffix == '.sat':
        return list(_sat_spans(text))
    if suffix in {'.stl', '.obj', '.dxf'}:
        if '\x00' in text:
            raise ValueError('Binary input requires a different handler')
        if suffix == '.stl' and not text.lstrip().startswith('solid'):
            raise ValueError('Not ASCII STL')
        return list(_line_spans(text, suffix))
    raise ValueError('Unsupported neutral format')


def extract(data: bytes, suffix: str) -> list[str]:
    """Return candidate metadata values; raise ValueError on unsupported syntax."""
    text, _ = _decode(data)
    return [value for _, _, value, _ in _spans(text, suffix.lower()) if value.strip()]


def transform(data: bytes, suffix: str, replace: Callable[[str], str]) -> tuple[bytes, dict]:
    """Change only identified metadata spans; return original bytes on parse error.

    ``replace`` must leave nonidentifying engineering text unchanged. Unsupported
    text encoding for a replacement is an error, never a lossy conversion.
    """
    suffix = suffix.lower()
    text, encoding = _decode(data)
    try:
        spans = _spans(text, suffix)
    except (ValueError, ET.ParseError) as error:
        return data, {'status': 'unsupported', 'reason': str(error), 'changes': 0}
    edits = []
    for start, end, value, kind in spans:
        updated = replace(value)
        if updated == value:
            continue
        if kind == 'step':
            updated = updated.replace("'", "''")
        elif kind == 'sat':
            if '\r' in updated or '\n' in updated:
                raise ValueError('Replacement cannot insert SAT header records')
            updated = str(len(updated.encode(encoding))) + ' ' + updated
        elif kind == 'xml':
            updated = html.escape(updated, quote=False)
        elif kind == 'cdata' and ']]>' in updated:
            raise ValueError('Replacement cannot terminate CDATA')
        elif kind == 'comment' and ('--' in updated or updated.endswith('-')):
            raise ValueError('Invalid XML comment replacement')
        elif kind == 'plain' and ('\r' in updated or '\n' in updated):
            raise ValueError('Replacement cannot insert format records')
        edits.append((start, end, updated))
    for start, end, value in reversed(edits):
        text = text[:start] + value + text[end:]
    output = text.encode(encoding)
    if suffix == '.svg':
        ET.fromstring(output)
    return output, {'status': 'processed', 'changes': len(edits), 'candidates': len(spans),
                    'limitations': 'Known metadata/text spans only; embedded images and unhandled fields require other handlers.'}
