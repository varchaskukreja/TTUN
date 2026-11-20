"""Consistent identity matching across paths, metadata, PDF text and OCR.

Underscores delimit filename tokens. Letters and digits do not: a registered
Alice matches Alice_part, but never Malice or Alice2. Longest aliases win.
"""
import re
from typing import Mapping


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
    pattern = re.compile(r'(?<![^\W_])(?:' + '|'.join(re.escape(k) for k in keys) + r')(?![^\W_])', re.IGNORECASE) if keys else None
    return pattern, normalized


def replace_identity_match(match, normalized):
    # Python IGNORECASE has a few additional Unicode equivalences (e.g. dotted
    # I) beyond casefold. Leave those unchanged unless explicitly registered.
    return normalized.get(match.group().casefold(), match.group())
