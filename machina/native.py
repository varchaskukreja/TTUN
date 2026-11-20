"""Conservative OLE CAD metadata inspection and length-preserving rewriting.

This does not edit geometry, proprietary containers, previews, or CAD references.
A successful rewrite is metadata coverage, never proof of complete de-identification.
"""
from __future__ import annotations

import re
import shutil
import struct
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Mapping
from .matching import compile_identity_mapping, replace_identity_match


@dataclass(frozen=True)
class PropertyString:
    section: int
    property_id: int
    text: str
    encoding: str
    offset: int
    size: int
    fmtid: str = ""
    semantic: str | None = None


# MS-OLEPS standard property sets; no title/subject/material inference.
SEMANTIC_FIELDS = {
    'f29f85e0-4ff9-1068-ab91-08002b27b3d9': {4: 'author', 8: 'last_saved_by'},
    'd5cdd502-2e9c-101b-9397-08002b2cf9ae': {14: 'manager', 15: 'company'},
    # Autodesk's published iProperties table, final page:
    # https://download.autodesk.com/us/community/mfg/part_4.pdf
    '32853f0f-3444-11d1-9e93-0060b03c1ca6': {
        10: 'checked_by', 12: 'engineering_approved_by', 30: 'vendor',
        34: 'manufacturing_approved_by', 41: 'designer', 42: 'engineer',
        48: 'manufacturer',
    },
}


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
        fmtid = str(uuid.UUID(bytes_le=bytes(data[28 + section * 20:44 + section * 20])))
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
            strings.append(PropertyString(section, pid, text, enc, pos + 8, byte_length - terminator, fmtid, SEMANTIC_FIELDS.get(fmtid, {}).get(pid)))
    return strings, issues


def rewrite_property_stream(data: bytes, mapping: Mapping[str, str]) -> tuple[bytes, dict]:
    """Repack scalar strings inside their existing OLE property-stream capacity.

    Property offsets/lengths are rebuilt, retaining every untouched value byte.
    Stream length and section start offsets never change. Only documented
    zero padding may be reclaimed. Unaffordable growth is explicitly rejected;
    shrinking changes are applied first to make space for larger pseudonyms.
    """
    fields, issues = property_strings(data)
    out = bytearray(data)
    changed, rejected = 0, 0
    pattern, normalized = compile_identity_mapping(mapping)
    pending = {}
    for field in fields:
        replacement = pattern.sub(lambda m: replace_identity_match(m, normalized), field.text) if pattern else field.text
        if replacement == field.text:
            continue
        try:
            encoded = replacement.encode(field.encoding)
        except UnicodeError:
            rejected += 1; continue
        pending.setdefault(field.section, []).append((field, encoded))
    if not pending:
        return data, {'changed_fields': 0, 'rejected_fields': rejected, 'issues': issues}
    count = struct.unpack_from('<I', data, 24)[0]
    bases = [struct.unpack_from('<I', data, 44 + i * 20)[0] for i in range(count)]
    for section, edits in pending.items():
        base = bases[section]
        size, n = struct.unpack_from('<II', data, base)
        entries = [struct.unpack_from('<II', data, base + 8 + i * 8) for i in range(n)]
        ordered = sorted(entries, key=lambda x: x[1])
        offsets = [off for _, off in ordered]
        # Do not rebuild aliased, overlapping, or unaligned property layouts.
        if (len(set(offsets)) != len(offsets) or not offsets or
            min(offsets) < 8 + 8*n or max(offsets) > size - 4 or
            any(off % 4 for off in offsets) or
            base < 28 + 20*count or
            any(other != base and base < other < base + size for other in bases) or
            bases.count(base) != 1):
            rejected += len(edits); issues.append('unsafe_property_layout'); continue
        ends = offsets[1:] + [size]
        blocks = {off: data[base + off:base + end] for off, end in zip(offsets, ends)}
        first = offsets[0]
        next_section = min((other for other in bases if other > base), default=len(data))
        capacity = size
        if next_section >= base + size and not any(data[base + size:next_section]):
            capacity = next_section - base
        proposals = []
        for field, encoded in edits:
            off = field.offset - 8 - base
            raw = blocks[off]
            kind = struct.unpack_from('<I', raw)[0]
            null = bytes(2 if field.encoding == 'utf-16-le' else 1)
            original_end = 8 + field.size + len(null)
            if len(encoded) == field.size:
                block = raw[:8] + encoded + raw[8 + field.size:]
            elif any(raw[original_end:]):
                rejected += 1; issues.append('nonzero_property_tail'); continue
            else:
                payload = encoded + null
                length = len(payload) // (2 if kind == 31 else 1)
                block = struct.pack('<II', kind, length) + payload
                block += bytes((-len(block)) % 4)
            proposals.append((len(block)-len(raw), off, block))
        used = first + sum(len(v) for v in blocks.values())
        accepted = 0
        for delta, off, block in sorted(proposals, key=lambda item: item[0]):
            if used + delta > capacity:
                rejected += 1; issues.append('property_stream_capacity_exceeded'); continue
            blocks[off] = block
            used += delta
            accepted += 1
        if not accepted:
            continue
        rebuilt = bytearray(data[base:base + first])
        new_offsets = {}
        for off in offsets:
            new_offsets[off] = len(rebuilt)
            rebuilt.extend(blocks[off])
        struct.pack_into('<I', rebuilt, 0, len(rebuilt))
        for i, (_, off) in enumerate(entries):
            struct.pack_into('<I', rebuilt, 12 + i*8, new_offsets[off])
        out[base:base + capacity] = rebuilt + bytes(capacity-len(rebuilt))
        changed += accepted
    result = bytes(out)
    parsed, after_issues = property_strings(result)
    # Every parsed nonedited property must retain its value and semantic type.
    original = {(f.section, f.property_id): f for f in fields}
    if len(result) != len(data) or len(parsed) != len(fields):
        raise ValueError('property_stream_rebuild_validation_failed')
    for field in parsed:
        old = original[(field.section, field.property_id)]
        wanted = pattern.sub(lambda m: replace_identity_match(m, normalized), old.text) if pattern else old.text
        if field.text not in (old.text, wanted) or field.encoding != old.encoding:
            raise ValueError('property_value_changed_unexpectedly')
    return result, {'changed_fields': changed, 'rejected_fields': rejected, 'issues': issues}


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
