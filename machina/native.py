"""Conservative OLE CAD metadata inspection and length-preserving rewriting.

This does not edit geometry, proprietary containers, previews, or CAD references.
A successful rewrite is metadata coverage, never proof of complete de-identification.
"""
from __future__ import annotations

import re
import shutil
import struct
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class PropertyString:
    section: int
    property_id: int
    text: str
    encoding: str
    offset: int
    size: int


def property_strings(data: bytes) -> tuple[list[PropertyString], list[str]]:
    """Parse only bounded scalar string values in MS-OLEPS property sets.

    Dictionary/property names (PID 0), code pages and Inventor property-set
    names (PID 255) are deliberately excluded from replacements.
    """
    strings, issues = [], []
    if len(data) < 28 or data[:2] != b'\xfe\xff':
        return strings, ['not_ole_property_set']
    u32 = lambda pos: struct.unpack_from('<I', data, pos)[0]
    count = u32(24)
    if count > 64 or 28 + 20 * count > len(data):
        return strings, ['invalid_section_table']
    for section in range(count):
        base = u32(28 + section * 20 + 16)
        if base + 8 > len(data):
            issues.append('invalid_section_offset'); continue
        size, n = u32(base), u32(base + 4)
        if size < 8 or base + size > len(data) or n > (size - 8) // 8:
            issues.append('invalid_property_table'); continue
        entries = [struct.unpack_from('<II', data, base + 8 + i * 8) for i in range(n)]
        offsets = sorted(set(off for _, off in entries) | {size})
        codepage = 1252
        for pid, off in entries:
            if pid == 1 and 8 + 8 * n <= off <= size - 6:
                codepage = struct.unpack_from('<H', data, base + off + 4)[0]
        for pid, off in entries:
            if pid in (0, 1, 255, 0x80000000):
                continue
            if off < 8 + 8 * n or off > size - 4:
                issues.append('invalid_property_offset'); continue
            end = base + next(x for x in offsets if x > off)
            pos = base + off
            kind = u32(pos)
            if kind not in (30, 31):
                if kind & 0x1000 or kind in (65, 71):
                    issues.append('unsupported_vector_or_blob')
                continue
            if pos + 8 > end:
                issues.append('truncated_string_header'); continue
            length = u32(pos + 4)
            enc = 'utf-16-le' if kind == 31 else ('utf-16-le' if codepage == 1200 else f'cp{codepage}')
            # LPSTR length is bytes; LPWSTR length is UTF-16 code units.
            byte_length = length * (2 if kind == 31 else 1)
            terminator = 2 if enc == 'utf-16-le' else 1
            if byte_length < terminator or pos + 8 + byte_length > end:
                issues.append('invalid_string_length'); continue
            payload = data[pos + 8:pos + 8 + byte_length]
            if payload[-terminator:] != bytes(terminator):
                issues.append('unterminated_string'); continue
            try:
                text = payload[:-terminator].decode(enc, errors='strict')
            except (UnicodeError, LookupError):
                issues.append('unsupported_string_encoding'); continue
            if '\x00' in text:
                issues.append('embedded_null_string'); continue
            strings.append(PropertyString(section, pid, text, enc, pos + 8, byte_length - terminator))
    return strings, issues


def compile_identity_mapping(mapping: Mapping[str, str]):
    """Normalize registry keys and match complete identities without cascading.

    Conflicting variants of the same casefolded identity are rejected rather
    than allowing mapping iteration order to choose a pseudonym.
    """
    normalized = {}
    for key, replacement in mapping.items():
        if not key:
            continue
        folded = key.casefold()
        if folded in normalized and normalized[folded] != replacement:
            raise ValueError('conflicting_casefold_identity_mapping')
        normalized[folded] = replacement
    keys = sorted(normalized, key=len, reverse=True)
    pattern = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(k) for k in keys) + r')(?!\w)', re.IGNORECASE) if keys else None
    return pattern, normalized


def replace_identity_match(match, normalized):
    # Python IGNORECASE has a few additional Unicode equivalences (e.g. dotted
    # I) beyond casefold. Leave those unchanged unless explicitly registered.
    return normalized.get(match.group().casefold(), match.group())


def rewrite_property_stream(data: bytes, mapping: Mapping[str, str]) -> tuple[bytes, dict]:
    """Replace exact identifier occurrences once, only inside parsed values.

    Rejects the entire field if its encoded size changes, avoiding truncation,
    padding identities inconsistently, and offset corruption.
    """
    fields, issues = property_strings(data)
    out = bytearray(data)
    changed, rejected = 0, 0
    pattern, normalized = compile_identity_mapping(mapping)
    for field in fields:
        replacement = pattern.sub(lambda m: replace_identity_match(m, normalized), field.text) if pattern else field.text
        if replacement == field.text:
            continue
        try:
            encoded = replacement.encode(field.encoding)
        except UnicodeError:
            rejected += 1; continue
        if len(encoded) != field.size:
            rejected += 1; continue
        out[field.offset:field.offset + field.size] = encoded
        changed += 1
    return bytes(out), {'changed_fields': changed, 'rejected_fields': rejected, 'issues': issues}


def inspect_native(path: str | Path) -> dict:
    import olefile
    report = {'format': 'unsupported', 'properties': [], 'issues': [], 'unsupported_streams': []}
    if not olefile.isOleFile(str(path)):
        report['issues'].append('proprietary_container_requires_vendor_adapter')
        return report
    report['format'] = 'ole'
    with olefile.OleFileIO(str(path)) as ole:
        for stream in ole.listdir():
            if not stream[-1].startswith('\x05'):
                report['unsupported_streams'].append(stream)
                continue
            fields, issues = property_strings(ole.openstream(stream).read())
            report['properties'].extend(dict(asdict(f), stream=stream) for f in fields)
            report['issues'].extend({'stream': stream, 'reason': issue} for issue in issues)
    return report


def rewrite_native(source: str | Path, destination: str | Path, mapping: Mapping[str, str]) -> dict:
    """Copy a native file and modify only recognized OLE scalar string fields.

    Returned reports contain no original property values. Input and output must
    differ. Non-property streams are preserved and explicitly remain unprocessed.
    """
    import olefile
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError('source and destination must differ')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    report = {'changed_fields': 0, 'rejected_fields': 0, 'issues': [], 'unprocessed_streams': 0,
              'complete_deidentification': False}
    if not olefile.isOleFile(str(source)):
        report['issues'].append('proprietary_container_requires_vendor_adapter')
        return report
    with olefile.OleFileIO(str(destination), write_mode=True) as ole:
        for stream in ole.listdir():
            if not stream[-1].startswith('\x05'):
                report['unprocessed_streams'] += 1
                continue
            data = ole.openstream(stream).read()
            patched, result = rewrite_property_stream(data, mapping)
            report['changed_fields'] += result['changed_fields']
            report['rejected_fields'] += result['rejected_fields']
            report['issues'].extend(result['issues'])
            if patched != data:
                ole.write_stream(stream, patched)
    return report
