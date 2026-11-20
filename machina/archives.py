"""Bounded in-memory archive rewriting without extracting filesystem paths.

Callback protocol: transform_member(name, bytes) -> (new_name, new_bytes, report).
For archive members the callback must rename only and return the original bytes;
this module then recursively rewrites the contents. It also calls the callback
for comments via synthetic .txt names. Unsupported/error member reports fail
closed. Successful container processing is not proof of complete de-ID: member
reports and coverage limitations remain available to the submission gate.
"""
from __future__ import annotations

import copy
import gzip
import io
import re
import stat
import struct
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable


class ArchiveError(ValueError):
    """Archive cannot safely be rewritten; never substitute the raw input."""


@dataclass(frozen=True)
class Limits:
    max_depth: int = 6
    max_members: int = 100_000
    max_member_bytes: int = 256 * 1024 * 1024
    max_expanded_bytes: int = 1024 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024 * 1024


@dataclass
class _Budget:
    members: int = 0
    expanded: int = 0


def kind(name: str, data: bytes) -> str | None:
    lower = name.lower()
    if data.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')):
        return 'zip'
    if data.startswith(b'\x1f\x8b'):
        return 'gzip'
    if lower.endswith('.tar') or len(data) >= 262 and data[257:262] == b'ustar':
        return 'tar'
    return None


def safe_name(name: str) -> str:
    if (not isinstance(name, str) or not name or '\x00' in name or '\\' in name
            or name.startswith('/') or re.match(r'^[A-Za-z]:', name)):
        raise ArchiveError('Unsafe archive member name')
    pieces = name.rstrip('/').split('/')
    if any(p in {'', '.', '..'} for p in pieces):
        raise ArchiveError('Unsafe archive member path component')
    return name


def _check_report(report):
    if not isinstance(report, dict):
        raise ArchiveError('Member callback must return a report dictionary')
    if report.get('status') in {'unsupported', 'error', 'failed', 'unprocessed', 'blocked'}:
        raise ArchiveError('Member callback reported an unprocessed member')


def transform(data: bytes, name: str, transform_member: Callable,
              *, limits: Limits | None = None) -> tuple[bytes, dict]:
    """Rewrite a ZIP, TAR or GZIP recursively, raising ArchiveError on failure.

    Limits apply cumulatively across nesting, including duplicate entries. No
    symlinks, hardlinks, devices, encrypted ZIP members, duplicate output paths,
    or traversal paths are accepted. Archives are never extracted to disk.
    """
    limits = limits or Limits()
    budget = _Budget()

    def read_bounded(stream, size=None):
        if size is not None and size > limits.max_member_bytes:
            raise ArchiveError('Member size limit exceeded')
        cap = min(limits.max_member_bytes, limits.max_expanded_bytes - budget.expanded)
        if cap < 0:
            raise ArchiveError('Expanded byte limit exceeded')
        content = stream.read(cap + 1)
        if len(content) > cap:
            raise ArchiveError('Expanded byte limit exceeded')
        if size is not None and len(content) != size:
            raise ArchiveError('Archive member size mismatch')
        budget.expanded += len(content)
        return content

    def call(member_name, content):
        new_name, updated, report = transform_member(member_name, content)
        safe_name(new_name)
        if not isinstance(updated, bytes):
            raise ArchiveError('Member callback must return bytes')
        if len(updated) > limits.max_member_bytes:
            raise ArchiveError('Rewritten member size limit exceeded')
        _check_report(report)
        return new_name, updated, report

    def metadata(content, label):
        if not content:
            return content, None
        _, updated, report = call(label + '.txt', content)
        return updated, report

    def member(member_name, content, depth, seen, directory=False):
        safe_name(member_name)
        budget.members += 1
        if budget.members > limits.max_members:
            raise ArchiveError('Archive member count limit exceeded')
        new_name, updated, report = call(member_name, content)
        if directory:
            new_name = new_name.rstrip('/') + '/'
            if updated:
                raise ArchiveError('Directory callback returned file content')
        key = new_name.rstrip('/')
        if key in seen:
            raise ArchiveError('Output archive member collision')
        # File/directory prefix conflicts make extraction order dependent.
        for ancestor in PurePosixPath(key).parents:
            if str(ancestor) in seen and seen[str(ancestor)] is False:
                raise ArchiveError('Output archive file/directory collision')
        if not directory and any(k.startswith(key + '/') for k in seen):
            raise ArchiveError('Output archive file/directory collision')
        seen[key] = directory
        nested = kind(member_name, content) if not directory else None
        if nested:
            if updated != content:
                raise ArchiveError('Archive callback must rename containers without changing their bytes')
            updated, child = rewrite(content, member_name, depth + 1)
            report = {'callback': report, 'archive': child}
        return new_name, updated, report

    def rewrite(content, archive_name, depth):
        if depth > limits.max_depth:
            raise ArchiveError('Archive nesting depth limit exceeded')
        archive_kind = kind(archive_name, content)
        report = {'format': archive_kind, 'status': 'processed', 'members': [], 'metadata': []}
        seen = {}
        destination = io.BytesIO()
        if archive_kind == 'zip':
            with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(destination, 'w') as target:
                for info in source.infolist():
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ArchiveError('ZIP special file rejected')
                    if info.flag_bits & 1:
                        raise ArchiveError('Encrypted ZIP member rejected')
                    safe_name(info.orig_filename)
                    safe_name(info.filename)
                    with source.open(info) as stream:
                        payload = read_bounded(stream, info.file_size)
                    renamed, payload, detail = member(info.filename, payload, depth, seen, info.is_dir())
                    output_info = copy.copy(info)
                    output_info.filename = renamed
                    output_info.orig_filename = renamed
                    output_info.comment, comment_report = metadata(info.comment, '__entry_comment__')
                    if comment_report:
                        report['metadata'].append(comment_report)
                    # Extra fields can carry stale Unicode paths or arbitrary
                    # producer metadata. Preserve only timestamps; zipfile writes
                    # fresh ZIP64 length records itself when necessary.
                    extra = bytearray(); pos = 0
                    while pos < len(info.extra):
                        if pos + 4 > len(info.extra):
                            raise ArchiveError('Malformed ZIP extra field')
                        tag, length = struct.unpack_from('<HH', info.extra, pos)
                        end = pos + 4 + length
                        if end > len(info.extra):
                            raise ArchiveError('Truncated ZIP extra field')
                        if tag in {0x5455, 0x000A}:
                            extra.extend(info.extra[pos:end])
                        pos = end
                    output_info.extra = bytes(extra)
                    target.writestr(output_info, payload)
                    if destination.tell() > limits.max_output_bytes:
                        raise ArchiveError('Archive output size limit exceeded')
                    report['members'].append({'path': renamed, 'report': detail})
                target.comment, comment_report = metadata(source.comment, '__archive_comment__')
                if comment_report:
                    report['metadata'].append(comment_report)
        elif archive_kind == 'tar':
            with tarfile.open(fileobj=io.BytesIO(content), mode='r:') as source, tarfile.open(fileobj=destination, mode='w', format=tarfile.PAX_FORMAT) as target:
                for info in source:
                    if not (info.isfile() or info.isdir()):
                        raise ArchiveError('TAR links and special files rejected')
                    safe_name(info.name)
                    stream = source.extractfile(info) if info.isfile() else io.BytesIO()
                    with stream:
                        payload = read_bounded(stream, info.size)
                    renamed, payload, detail = member(info.name, payload, depth, seen, info.isdir())
                    output_info = copy.copy(info)
                    output_info.name, output_info.size = renamed, len(payload)
                    for field in ('uname', 'gname'):
                        updated, meta_report = metadata(getattr(info, field).encode('utf-8'), '__tar_' + field + '__')
                        setattr(output_info, field, updated.decode('utf-8'))
                        if meta_report:
                            report['metadata'].append(meta_report)
                    output_info.pax_headers = {}
                    for key, value in info.pax_headers.items():
                        if key in {'path', 'linkpath', 'size', 'uname', 'gname'}:
                            continue  # Regenerated from sanitized TarInfo.
                        if key in {'mtime', 'atime', 'ctime', 'uid', 'gid'}:
                            output_info.pax_headers[key] = value
                        else:
                            # Unknown extended attributes may hide raw identities.
                            raise ArchiveError('Unsupported TAR extended metadata')
                    target.addfile(output_info, io.BytesIO(payload) if info.isfile() else None)
                    if destination.tell() > limits.max_output_bytes:
                        raise ArchiveError('Archive output size limit exceeded')
                    report['members'].append({'path': renamed, 'report': detail})
        elif archive_kind == 'gzip':
            with gzip.GzipFile(fileobj=io.BytesIO(content), mode='rb') as source:
                payload = read_bounded(source)
            lower = archive_name.lower()
            inner_name = archive_name[:-4] + '.tar' if lower.endswith('.tgz') else archive_name[:-3] if lower.endswith('.gz') else 'content'
            inner_name = PurePosixPath(inner_name).name
            renamed, payload, detail = member(inner_name, payload, depth, seen)
            # GZIP optional original filename/comment are intentionally omitted.
            destination.write(gzip.compress(payload, mtime=0))
            report['members'].append({'path': renamed, 'report': detail})
        else:
            raise ArchiveError('Unsupported archive format')
        output = destination.getvalue()
        if len(output) > limits.max_output_bytes:
            raise ArchiveError('Archive output size limit exceeded')
        return output, report

    try:
        output, report = rewrite(data, name, 0)
    except ArchiveError:
        raise
    except (OSError, EOFError, zipfile.BadZipFile, tarfile.TarError, RuntimeError, ValueError) as error:
        # Parser errors can contain original filenames; keep diagnostics neutral.
        raise ArchiveError('Invalid archive or callback failure: ' + type(error).__name__) from None
    report['total_members'] = budget.members
    report['expanded_bytes'] = budget.expanded
    return output, report
