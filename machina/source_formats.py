"""Identity editing in source comments/literals, never executable identifiers.

Python is parsed before/after and edited using tokenizer positions. JSON values
are edited with keys and numeric types preserved. OpenSCAD handles comments,
quoted strings, and include/use paths with a lexer; it does not evaluate CAD.
Unsupported identifying surfaces fail closed instead of claiming sanitization.
"""
from __future__ import annotations
import ast
import io
import json
import re
import tokenize

SUPPORTED = {'.py', '.pyi', '.json', '.scad'}


def _result(data, edits, encoding, report):
    text = data
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    report['changes'] = len(edits)
    return text.encode(encoding), report


def transform_python(data: bytes, replace):
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    text = data.decode(encoding)
    ast.parse(text)
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    edits = []
    issues = set()
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        updated = token.string
        if token.type == tokenize.COMMENT:
            updated = replace(token.string)
            if '\n' in updated or '\r' in updated:
                raise ValueError('Comment replacement cannot insert source records')
            if token.start[0] <= 2 and re.search(r'coding\s*[:=]', token.string):
                # Encoding declaration is required syntax, not personal metadata.
                if updated != token.string:
                    issues.add('identifying_encoding_declaration')
                updated = token.string
        elif token.type == tokenize.STRING:
            try:
                value = ast.literal_eval(token.string)
            except (SyntaxError, ValueError):
                if replace(token.string) != token.string:
                    issues.add('identifying_interpolated_string')
                continue
            if isinstance(value, str):
                new_value = replace(value)
                if new_value != value:
                    updated = repr(new_value)
            elif isinstance(value, bytes):
                # Byte literals may contain serialized engineering data, not text.
                try:
                    if replace(value.decode('utf-8')) != value.decode('utf-8'):
                        issues.add('identifying_byte_literal')
                except UnicodeDecodeError:
                    pass
        elif tokenize.tok_name.get(token.type, '').startswith('FSTRING'):
            if replace(token.string) != token.string:
                issues.add('identifying_interpolated_string')
        elif token.type == tokenize.NAME:
            if replace(token.string) != token.string:
                issues.add('identifying_executable_identifier')
        if updated != token.string:
            start = offsets[token.start[0] - 1] + token.start[1]
            end = offsets[token.end[0] - 1] + token.end[1]
            edits.append((start, end, updated))
    output, report = _result(text, edits, encoding, {
        'status': 'unsupported' if issues else 'processed', 'format': 'python',
        'issues': sorted(issues), 'limitations': ['known_comments_and_string_values_only', 'import_identifier_rewrites_require_project_analysis']})
    ast.parse(output.decode(encoding))
    return output, report


def _load_json(text):
    def reject(value):
        raise ValueError('Nonstandard JSON numeric constant')
    return json.loads(text, parse_constant=reject)


def transform_json(data: bytes, replace):
    text = data.decode('utf-8-sig')
    _load_json(text)
    edits = []; issues = set()
    for match in re.finditer(r'"(?:[^"\\]|\\.)*"', text, re.S):
        value = json.loads(match.group())
        updated = replace(value)
        if updated == value:
            continue
        # Object keys may encode schema or reference maps. Do not silently rename.
        following = text[match.end():].lstrip()
        if following.startswith(':'):
            issues.add('identifying_json_key_requires_reference_analysis')
            continue
        edits.append((match.start(), match.end(), json.dumps(updated, ensure_ascii=True)))
    encoding = 'utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'utf-8'
    output, report = _result(text, edits, encoding, {
        'status': 'unsupported' if issues else 'processed', 'format': 'json',
        'issues': sorted(issues), 'limitations': ['known_string_values_only']})
    _load_json(output.decode(encoding))
    return output, report


_SCAD_TOKEN = re.compile(r'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:[^"\\]|\\.)*"|[A-Za-z_$][A-Za-z_0-9$]*|\s+|.', re.S)
_SCAD_ESCAPES = {'n': '\n', 'r': '\r', 't': '\t', '"': '"', '\\': '\\'}


def _scad_decode(raw):
    # OpenSCAD also accepts \uNNNN / \UNNNNNNNN Unicode escapes.
    pieces = []; i = 0
    while i < len(raw):
        if raw[i] != '\\':
            pieces.append(raw[i]); i += 1; continue
        if i + 1 >= len(raw):
            raise ValueError('Truncated OpenSCAD escape')
        code = raw[i + 1]
        if code in _SCAD_ESCAPES:
            pieces.append(_SCAD_ESCAPES[code]); i += 2
        elif code in {'u', 'U'}:
            width = 4 if code == 'u' else 8
            digits = raw[i + 2:i + 2 + width]
            if len(digits) != width or not re.fullmatch('[0-9A-Fa-f]+', digits):
                raise ValueError('Invalid OpenSCAD Unicode escape')
            pieces.append(chr(int(digits, 16))); i += 2 + width
        else:
            # Preserve unknown escape semantics by declining this string.
            raise ValueError('Unknown OpenSCAD escape')
    return ''.join(pieces)


def transform_scad(data: bytes, replace, rename=None):
    text = data.decode('utf-8-sig')
    rename = rename or replace
    edits = []; issues = set(); pending_include = False
    tokens = iter(_SCAD_TOKEN.finditer(text))
    for token in tokens:
        raw = token.group()
        if raw.isspace():
            continue
        if raw.startswith('//'):
            updated = replace(raw[2:])
            if '\r' in updated or '\n' in updated:
                raise ValueError('Comment replacement cannot insert source records')
            if updated != raw[2:]:
                edits.append((token.start()+2, token.end(), updated))
            continue
        if raw.startswith('/*'):
            updated = replace(raw[2:-2])
            if '*/' in updated:
                raise ValueError('Replacement cannot terminate source comment')
            if updated != raw[2:-2]:
                edits.append((token.start()+2, token.end()-2, updated))
            continue
        if pending_include and raw == '<':
            start = token.end(); end = None
            for closing in tokens:
                if closing.group() == '>':
                    end = closing.start(); break
            if end is None:
                raise ValueError('Unterminated OpenSCAD include path')
            value = text[start:end]; updated = rename(value)
            if any(c in updated for c in '<>\r\n'):
                raise ValueError('Invalid OpenSCAD include replacement')
            if updated != value:
                edits.append((start,end,updated))
            pending_include = False
            continue
        pending_include = raw in {'include', 'use'}
        if raw.startswith('"'):
            try:
                value = _scad_decode(raw[1:-1])
            except ValueError:
                issues.add('unparsed_scad_string_escape')
                continue
            updated = replace(value)
            if updated != value:
                # UTF-8 is native to SCAD; avoid surrogate-pair escapes.
                edits.append((token.start(), token.end(), json.dumps(updated, ensure_ascii=False)))
        elif re.fullmatch('[A-Za-z_$][A-Za-z_0-9$]*', raw) and replace(raw) != raw:
            issues.add('identifying_executable_identifier')
    encoding = 'utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'utf-8'
    return _result(text, edits, encoding, {
        'status': 'unsupported' if issues else 'processed', 'format': 'openscad',
        'issues': sorted(issues), 'limitations': ['known_comments_strings_and_include_paths_only', 'openscad_render_required']})


def transform(data: bytes, suffix: str, replace, *, rename=None):
    suffix = suffix.lower()
    if suffix in {'.py', '.pyi'}:
        return transform_python(data, replace)
    if suffix == '.json':
        return transform_json(data, replace)
    if suffix == '.scad':
        return transform_scad(data, replace, rename)
    return data, {'status': 'unsupported', 'format': 'source', 'issues': ['unsupported_source_language']}
