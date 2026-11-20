"""FreeCAD member handlers with geometry and schema preservation.

Format evidence: FreeCAD src/App/PropertyStandard.cpp (PropertyColorList and
PropertyMaterialList SaveDocFile), and OpenCascade BRepTools ASCII format.
These handlers cover known identities only; a native FreeCAD reopen/recompute
and thumbnail review are still required before submission.
"""
from __future__ import annotations
import html
import io
import re
import struct
import xml.etree.ElementTree as ET
import zipfile

_BREP_WORDS = set('CASCADE Topology V c Matra Datavision Locations Curve Curves Polygon PolygonOnTriangulations Triangulations Surfaces TShapes C D G ds Co Cs So Sh Fa Wi Ed Ve CN e E'.split())
_TOKEN = re.compile(r'''<!\[CDATA\[.*?\]\]>|<!--.*?-->|<(?:[^>"']|"[^"]*"|'[^']*')*>|[^<]+''', re.S)
_ATTR = re.compile(r'''([\w:.-]+)(\s*=\s*)(["'])(.*?)\3''', re.S)


def member_types(data: bytes) -> dict[str, dict]:
    """Read native property types from both XML documents, without extraction."""
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in ('Document.xml', 'GuiDocument.xml'):
            if name not in archive.namelist():
                continue
            if archive.getinfo(name).file_size > 32 * 1024 * 1024:
                raise ValueError('FreeCAD XML exceeds inspection limit')
            root = ET.fromstring(archive.read(name))
            for prop in root.iter('Property'):
                for node in prop.iter():
                    filename = node.get('file')
                    if filename:
                        record = {'type': prop.get('type'), 'version': node.get('version', '0'), 'element': node.tag}
                        if filename in result and result[filename] != record:
                            raise ValueError('Conflicting FreeCAD member declarations')
                        result[filename] = record
    return result


def transform_xml(data: bytes, replace, rename=None):
    """Edit values/references, preserving schema keys and geometry attributes."""
    rename = rename or replace
    text = data.decode('utf-8-sig')
    if re.search(r'<!\s*(DOCTYPE|ENTITY)', text, re.I):
        raise ValueError('FreeCAD XML entities are unsupported')
    root = ET.fromstring(text)
    if root.tag not in {'Document', 'GuiDocument'}:
        raise ValueError('Not a FreeCAD document XML')
    edits = 0
    def substitute(match):
        nonlocal edits
        token = match.group()
        if token.startswith('<!--'):
            value = replace(token[4:-3])
            if '--' in value or value.endswith('-'):
                raise ValueError('Invalid comment replacement')
            edits += value != token[4:-3]
            return '<!--' + value + '-->'
        if token.startswith('<![CDATA['):
            value = replace(token[9:-3])
            if ']]>' in value:
                raise ValueError('Invalid CDATA replacement')
            edits += value != token[9:-3]
            return '<![CDATA[' + value + ']]>'
        if token.startswith('<?') or token.startswith('</'):
            return token
        if token.startswith('<'):
            tag = re.match(r'<\s*([^\s/>]+)', token)[1]
            def attribute(m):
                nonlocal edits
                key, spacing, quote, raw = m.groups()
                value = html.unescape(raw)
                # Property names/types are programmatic schema, not display names.
                if key in {'type', 'typeId', 'SchemaVersion', 'ProgramVersion', 'FileVersion', 'xmlns', 'uuid'} or (tag in {'Property', 'Extension'} and key == 'name'):
                    return m.group()
                if key == 'file':
                    updated = rename(value) if value else value
                elif (key in {'name', 'value', 'string', 'content', 'expression', 'label', 'path', 'filename'}
                      and not re.fullmatch(r'[+\-\d.eE\s,]*', value)):
                    updated = replace(value)
                else:
                    return m.group()
                if updated == value:
                    return m.group()
                edits += 1
                return key + spacing + quote + html.escape(updated, quote=True) + quote
            return _ATTR.sub(attribute, token)
        updated = replace(html.unescape(token)) if token.strip() else token
        if updated != html.unescape(token):
            edits += 1
            return html.escape(updated, quote=False)
        return token
    output = _TOKEN.sub(substitute, text).encode('utf-8')
    if data.startswith(b'\xef\xbb\xbf'):
        output = b'\xef\xbb\xbf' + output
    ET.fromstring(output)
    return output, {'status': 'processed', 'changes': edits, 'limitations': ['known_identities_only', 'native_reopen_required']}


def transform_material(data, replace, version='3'):
    """Keep packed colors/floats exact; rewrite v3 texture strings with lengths."""
    if len(data) < 4:
        raise ValueError('Truncated FreeCAD material list')
    count = struct.unpack_from('<I', data)[0]
    end = 4 + count * 24
    if count > 1_000_000 or end > len(data):
        raise ValueError('Invalid FreeCAD material count')
    if version in {'0', '1', '2'}:
        if end != len(data):
            raise ValueError('Unsupported legacy material list variant')
        return data, {'status': 'processed', 'changes': 0, 'geometry_bytes_preserved': True}
    if version != '3':
        raise ValueError('Unsupported FreeCAD material version')
    output = bytearray(data[:end]); pos = end; changes = 0
    for _ in range(count):
        for field in ('image', 'imagePath', 'uuid'):
            if pos + 4 > len(data):
                raise ValueError('Truncated material string length')
            size = struct.unpack_from('<I', data, pos)[0]; pos += 4
            if pos + size > len(data):
                raise ValueError('Truncated material string')
            raw = data[pos:pos + size]; pos += size
            value = raw.decode('utf-8')
            # Material UUID is an engineering reference, not a personal identity.
            updated = (value if field == 'uuid' else replace(value)).encode('utf-8')
            changes += updated != raw
            output.extend(struct.pack('<I', len(updated))); output.extend(updated)
    if pos != len(data):
        raise ValueError('Unexpected material trailing data')
    return bytes(output), {'status': 'processed', 'changes': changes, 'geometry_bytes_preserved': True,
                          'limitations': ['texture_image_contents_require_review']}


def transform_member(name: str, data: bytes, replace, *, info=None, rename=None):
    """Return (bytes, report). Unknown members explicitly return unsupported.

    Pass member_types(archive_bytes).get(name) as info; typing comes from native
    XML, so arbitrary extensionless bytes never receive geometry pass-through.
    """
    info = info or {}
    if name in {'Document.xml', 'GuiDocument.xml'}:
        return transform_xml(data, replace, rename)
    typ = info.get('type')
    if typ == 'App::PropertyColorList':
        if len(data) < 4 or len(data) != 4 + 4 * struct.unpack_from('<I', data)[0]:
            raise ValueError('Invalid FreeCAD packed color array')
        return data, {'status': 'processed', 'changes': 0, 'geometry_bytes_preserved': True}
    if typ == 'App::PropertyMaterialList':
        return transform_material(data, replace, info.get('version', '0'))
    if typ == 'Part::PropertyPartShape' and name.lower().endswith('.brp'):
        if not data:
            return data, {'status': 'processed', 'changes': 0, 'geometry_bytes_preserved': True, 'empty_shape': True}
        if not re.match(rb'\s*CASCADE Topology V[123], \(c\) Matra-Datavision\s', data):
            raise ValueError('Unsupported FreeCAD BREP header')
        text = data.decode('ascii')
        if set(re.findall(r'[A-Za-z_]+', text)) - _BREP_WORDS:
            raise ValueError('Unknown text in BREP geometry')
        return data, {'status': 'processed', 'changes': 0, 'geometry_bytes_preserved': True,
                      'limitations': ['brep_native_parse_required']}
    return data, {'status': 'unsupported', 'complete_deidentification': False}
