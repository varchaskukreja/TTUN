"""Parse and apply unified diffs against the immutable baseline in memory."""
import re


def parse_patch(text):
    files = []
    current = None
    hunk = None
    for line in text.splitlines(keepends=True):
        if line.startswith('diff --git '):
            current = {'path': None, 'hunks': []}
            files.append(current)
            hunk = None
        elif line.startswith('+++ '):
            if current is None:
                raise ValueError('missing file header')
            path = line[4:].strip().split('\t')[0]
            current['path'] = path[2:] if path.startswith('b/') else path
        elif line.startswith('@@ '):
            match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if not match or current is None:
                raise ValueError('invalid hunk')
            hunk = {'start': int(match[1]), 'count': int(match[2] or 1), 'new_count': int(match[4] or 1), 'lines': []}
            current['hunks'].append(hunk)
        elif hunk is not None and line[:1] in (' ', '+', '-'):
            hunk['lines'].append(line)
        elif line.startswith('\\ No newline') and hunk and hunk['lines']:
            hunk['lines'][-1] = hunk['lines'][-1].rstrip('\n')
    if not files or any(not f['path'] or not f['hunks'] for f in files):
        raise ValueError('unsupported patch')
    return files


def apply_file(source, change):
    lines = source.splitlines(keepends=True)
    output, cursor, changed = [], 0, set()
    for hunk in change['hunks']:
        start = hunk['start'] - 1 if hunk['count'] else hunk['start']
        if start < cursor or start > len(lines):
            raise ValueError('overlapping hunks')
        output.extend(lines[cursor:start])
        cursor = start
        consumed = produced = 0
        for line in hunk['lines']:
            if line[0] in ' -':
                if cursor >= len(lines) or lines[cursor] != line[1:]:
                    raise ValueError('patch does not match baseline')
                if line[0] == '-':
                    changed.add(cursor + 1)
                else:
                    output.append(line[1:])
                    produced += 1
                cursor += 1
                consumed += 1
            else:
                output.append(line[1:])
                produced += 1
                changed.update((max(1, cursor), min(len(lines), cursor + 1)))
        if consumed != hunk['count'] or produced != hunk['new_count']:
            raise ValueError('incorrect hunk size')
    output.extend(lines[cursor:])
    return ''.join(output), changed
