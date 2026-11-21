"""Research whole-patch qualifier with exact linear text/token edit recognition.

Preserves the explicit qualification criterion, while eliminating incidental
SequenceMatcher false rejections on repetitive source. Does not authorize
narrowing or alter baseline observation/capture semantics.
"""
import importlib.util
import hashlib
from pathlib import Path
import tokenize

from comparison_types import observed_certificate
from linear_operator_edit import exact_operator_edit

_SPEC = importlib.util.spec_from_file_location('_fast_comparison_patches', Path(__file__).resolve().parents[1] / 'patches.py')
_PATCHES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PATCHES)


def qualify_patch(mapping, source_root, patch):
    rejected = {'equivalent_for_observed_types': False, 'reason': 'unsupported whole patch'}
    if any(line.startswith(('rename ', 'copy ', 'old mode ', 'new mode ', 'new file ', 'deleted file ', 'GIT binary patch')) for line in patch.splitlines()):
        return rejected
    try:
        changes = _PATCHES.parse_patch(patch)
        if len(changes) != 1:
            return rejected
        change = changes[0]
        path = change['path']
        root = Path(source_root).resolve()
        filename = (root / path).resolve()
        if not filename.is_relative_to(root) or filename.suffix != '.py':
            return rejected
        before = filename.read_text()
        after, _ = _PATCHES.apply_file(before, change)
        if exact_operator_edit(path, before, after) is None:
            return rejected
        return dict(observed_certificate(mapping, path, before, after), path=path)
    except (ValueError, SyntaxError, OSError, tokenize.TokenError):
        return rejected


def qualifier_hashes():
    """Bind postprocessing separately from the immutable capture core."""
    directory = Path(__file__).resolve().parent
    files = {name: directory / name for name in ('comparison_types.py', 'fast_comparison_qualifier.py', 'linear_operator_edit.py')}
    files['patches.py'] = directory.parent / 'patches.py'
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
