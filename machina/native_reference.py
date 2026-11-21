"""Private native-reference inventory and conservative length-preserving edits.

CString framing is based on Microsoft's MFC CArchive Unicode serialization;
SolidWorks usage independently documented by https://github.com/KenM76/swformat.
No class-object serialization, geometry, or unknown binary values are rewritten.
Candidate text is sensitive and must remain in private reports.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
import re
import struct
from .matching import compile_identity_mapping, replace_identity_match

CSTRING = b'\xff\xfe\xff'
MODEL_EXTENSIONS = ('.sldprt', '.sldasm', '.slddrw', '.sldlfp', '.slddrt', '.prtdot', '.asmdot', '.drwdot', '.ipt', '.iam', '.idw')


@dataclass(frozen=True)
class NativeString:
    offset: int
    size: int
    text: str
    encoding: str = 'utf-16-le'
    framing: str = 'mfc_cstring'


def cstrings(data: bytes):
    """Yield bounded Unicode CStrings, including 16/32-bit count escapes."""
    cursor = 0
    while True:
        start = data.find(CSTRING, cursor)
        if start < 0:
            return
        cursor = start + 3
        if cursor >= len(data):
            return
        length = data[cursor]; cursor += 1
        if length == 255:
            if cursor + 2 > len(data): return
            length = struct.unpack_from('<H', data, cursor)[0]; cursor += 2
            if length == 65535:
                if cursor + 4 > len(data): return
                length = struct.unpack_from('<I', data, cursor)[0]; cursor += 4
        if length > 32768 or cursor + 2*length > len(data):
            continue
        end = cursor + 2*length
        try:
            text = data[cursor:end].decode('utf-16-le')
        except UnicodeDecodeError:
            continue
        if not text or any(ord(c) < 32 for c in text):
            continue
        yield NativeString(cursor, 2*length, text)
        cursor = end


def is_reference(value: str) -> bool:
    if any(c in value for c in ('\x00', '$PRP', '"', '<', '>')):
        return False
    return (value.casefold().endswith(MODEL_EXTENSIONS) or
            bool(re.match(r'^[A-Za-z]:[\\/]', value)) or
            value.startswith(('\\\\', '/Users/', '/home/')))


def reference_strings(data: bytes):
    return [field for field in cstrings(data) if is_reference(field.text)]


def text_candidates(data: bytes, *, limit=5000):
    """Read-only ASCII/UTF16 runs for private contact/account/leak discovery.

    Unframed runs are evidence only and are never used for mutation.
    """
    count = 0
    for pattern, encoding in ((rb'[\x20-\x7e]{5,}', 'ascii'), (rb'(?:[\x20-\x7e]\x00){5,}', 'utf-16-le')):
        for match in re.finditer(pattern, data):
            raw = match.group()
            if len(raw) > 32768:
                continue
            yield NativeString(match.start(), len(raw), raw.decode(encoding), encoding, 'unframed_read_only')
            count += 1
            if count >= limit:
                return


def rewrite_reference_strings(data: bytes, mapping):
    pattern, normalized = compile_identity_mapping(mapping)
    result = bytearray(data)
    changed = rejected = 0
    for field in reference_strings(data):
        replacement = pattern.sub(lambda m: replace_identity_match(m, normalized), field.text) if pattern else field.text
        if replacement == field.text:
            continue
        encoded = replacement.encode(field.encoding)
        if len(encoded) != field.size:
            rejected += 1
            continue
        result[field.offset:field.offset + field.size] = encoded
        changed += 1
    return bytes(result), {'changed_references': changed, 'rejected_references': rejected}


def inspect_ole_streams(path: str | Path, mapping=None, *, max_stream_bytes=128*1024*1024, limit_per_stream=5000):
    """Read every bounded OLE stream, report candidate text without any edits."""
    import olefile
    result = {'format': 'ole', 'streams': [], 'issues': [], 'complete_inspection': True}
    if not olefile.isOleFile(str(path)):
        return {'format': 'unsupported', 'streams': [], 'issues': ['not_ole'], 'complete_inspection': False}
    pattern, normalized = compile_identity_mapping(mapping or {})
    with olefile.OleFileIO(str(path)) as ole:
        for stream in ole.listdir():
            size = ole.get_size(stream)
            if size > max_stream_bytes:
                result['issues'].append('stream_size_limit'); result['complete_inspection'] = False
                continue
            data = ole.openstream(stream).read()
            fields = list(text_candidates(data, limit=limit_per_stream+1))
            if len(fields)>limit_per_stream:
                result['complete_inspection'] = False
                result['issues'].append('stream_candidate_limit')
            fields = fields[:limit_per_stream]
            matches = sum(sum(1 for _ in pattern.finditer(f.text)) for f in fields) if pattern else 0
            result['streams'].append({'stream': stream, 'bytes': size,
                                      'known_identity_matches': matches,
                                      'candidates': [asdict(f) for f in fields]})
    # Compressed content may not expose strings; this is an explicit coverage
    # boundary even when every stored stream has been read.
    result['issues'].append('embedded_compression_not_decoded')
    result['complete_deidentification'] = False
    return result
