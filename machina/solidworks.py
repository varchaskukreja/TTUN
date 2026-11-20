"""Bounded modern SolidWorks container reader and conservative XML metadata edits.

Format research references (no third-party implementation copied):
https://github.com/schwitters/openswx (MIT)
https://github.com/KenM76/swformat (Apache-2.0)
The chunk layout, rotated names and span-preservation strategy were checked
against real corpus samples. Vendor reopen validation is still required.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
import re
import struct
import zlib
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, unescape
from .native import compile_identity_mapping, replace_identity_match

MARKER = b'\x14\x00\x06\x00\x08\x00'
MAX_STREAM = 128 * 1024 * 1024


@dataclass(frozen=True)
class Stream:
    offset: int
    data_offset: int
    compressed_size: int
    size: int
    crc: int
    name: str
    payload: bytes

    @property
    def end(self):
        return self.data_offset + self.compressed_size


def _name(raw: bytes, key: int) -> str:
    key &= 7
    decoded = bytes(((x << key) | (x >> (8 - key))) & 255 if key else x for x in raw)
    name = decoded.decode('ascii')
    if not name or not all(32 <= ord(c) <= 126 for c in name):
        raise ValueError('invalid_stream_name')
    return name


def read_streams(data: bytes) -> tuple[list[Stream], list[str]]:
    """Inventory verified raw-DEFLATE streams; reject size/CRC mismatches."""
    streams, issues = [], []
    if len(data) < 8 or MARKER not in data[:64]:
        return streams, ['not_modern_solidworks']
    pos = 8
    while True:
        marker = data.find(MARKER, pos)
        if marker < 0:
            break
        pos = marker + 6
        start = marker - 4
        if start < 8 or start + 30 > len(data):
            continue
        crc, csz, usz, nsz = struct.unpack_from('<IIII', data, start + 14)
        if not (0 < nsz <= 512 and 0 < csz <= MAX_STREAM and usz <= MAX_STREAM):
            continue
        payload_start = start + 30 + nsz
        if payload_start + csz > len(data):
            continue
        try:
            name = _name(data[start + 30:payload_start], data[7])
        except (ValueError, UnicodeError):
            # Directory entries have a different name offset.
            continue
        try:
            decoder = zlib.decompressobj(-15)
            payload = decoder.decompress(data[payload_start:payload_start + csz], usz + 1)
            if not decoder.eof or len(payload) != usz:
                issues.append('stream_size_or_deflate_error'); continue
            if zlib.crc32(payload) != crc:
                issues.append('stream_crc_error'); continue
        except zlib.error:
            issues.append('unsupported_cache_record' if crc < 65536 else 'stream_deflate_error'); continue
        streams.append(Stream(start, payload_start, csz, usz, crc, name, payload))
        pos = payload_start + csz
    return streams, issues


def _directory_entries(data: bytes, streams: list[Stream], target: Stream) -> list[int]:
    result = []
    pos = 8
    intervals = [(s.offset, s.end) for s in streams]
    while True:
        m = data.find(MARKER, pos)
        if m < 0:
            return result
        pos = m + 6
        start = m - 4
        if start < 8 or start + 44 > len(data):
            continue
        if any(a <= start < b for a, b in intervals):
            continue
        crc, csz, usz, nsz = struct.unpack_from('<IIII', data, start + 14)
        if not (0 < nsz <= 512) or start + 44 + nsz > len(data):
            continue
        try:
            name = _name(data[start + 44:start + 44 + nsz], data[7])
        except (ValueError, UnicodeError):
            continue
        pointer = struct.unpack_from('<I', data, start + 40)[0] + 8
        if name == target.name and pointer == target.offset and (crc, csz, usz) == (target.crc, target.compressed_size, target.size):
            result.append(start)


def _edit_xml(payload: bytes, mapping: Mapping[str, str]) -> bytes:
    """Edit text values only, retaining XML markup, namespaces and keys."""
    text = payload.decode('utf-8')
    ET.fromstring(text)
    pattern, normalized = compile_identity_mapping(mapping)
    if pattern is None:
        return payload
    def value(match):
        original = match.group(1)
        decoded = unescape(original, {'&quot;': '"', '&apos;': "'"})
        changed = pattern.sub(lambda m: replace_identity_match(m, normalized), decoded)
        return '>' + (escape(changed) if changed != decoded else original) + '<'
    changed = re.sub(r'>([^<>]*)<', value, text)
    ET.fromstring(changed)
    return changed.encode('utf-8')


def rewrite_bytes(data: bytes, mapping: Mapping[str, str]) -> tuple[bytes, dict]:
    """Edit docProps XML with fixed compressed spans and synchronized CRCs.

    Every other stream and gap remains byte-identical except matching directory
    CRC/decoded-size fields. Overflow is refused rather than shifting offsets.
    """
    streams, issues = read_streams(data)
    report = {'changed_streams': 0, 'rejected_streams': 0, 'issues': list(issues),
              'unprocessed_streams': 0, 'complete_deidentification': False,
              'vendor_reopen_verified': False}
    output = bytearray(data)
    for stream in streams:
        if not stream.name.startswith('docProps/') or not stream.name.endswith('.xml'):
            report['unprocessed_streams'] += 1
            continue
        try:
            changed = _edit_xml(stream.payload, mapping)
        except (UnicodeError, ET.ParseError):
            report['issues'].append('invalid_metadata_xml')
            report['rejected_streams'] += 1
            continue
        if changed == stream.payload:
            continue
        entries = _directory_entries(data, streams, stream)
        if len(entries) != 1:
            report['issues'].append('missing_or_ambiguous_directory_entry')
            report['rejected_streams'] += 1
            continue
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        compressed = compressor.compress(changed) + compressor.flush()
        if len(compressed) > stream.compressed_size:
            report['issues'].append('compressed_span_overflow')
            report['rejected_streams'] += 1
            continue
        crc = zlib.crc32(changed)
        output[stream.data_offset:stream.end] = compressed + bytes(stream.compressed_size - len(compressed))
        for start in (stream.offset, entries[0]):
            struct.pack_into('<I', output, start + 14, crc)
            struct.pack_into('<I', output, start + 22, len(changed))
        report['changed_streams'] += 1
    result = bytes(output)
    checked, errors = read_streams(result)
    if len(result) != len(data) or errors != issues or len(checked) != len(streams):
        raise ValueError('solidworks_rewrite_verification_failed')
    for before, after in zip(streams, checked):
        if (before.name, before.offset, before.end) != (after.name, after.offset, after.end):
            raise ValueError('solidworks_stream_layout_changed')
    return result, report


def rewrite_solidworks(source: str | Path, destination: str | Path, mapping: Mapping[str, str]) -> dict:
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError('source and destination must differ')
    result, report = rewrite_bytes(source.read_bytes(), mapping)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(result)
    return report
