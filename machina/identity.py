"""Explicit identity registry; private originals never belong in the submission."""
from __future__ import annotations
import hashlib
import hmac
import re
import unicodedata

KINDS = {'person', 'company', 'project', 'part', 'account', 'email', 'domain', 'address', 'phone'}

class Registry:
    def __init__(self, entries: list[dict], key: bytes):
        if len(key) < 16:
            raise ValueError('Use a private pseudonym key of at least 16 bytes')
        self.mapping = {}
        owners = {}
        for entry in entries:
            kind = entry['kind']
            if kind not in KINDS:
                raise ValueError(f'Unknown identity kind: {kind}')
            canonical = unicodedata.normalize('NFKC', entry['identity']).casefold().strip()
            if not canonical:
                raise ValueError('Empty identity')
            digest = hmac.new(key, (kind + ':' + canonical).encode(), hashlib.sha256).hexdigest()[:16]
            pseudonym = entry.get('pseudonym') or f'{kind.title()}-{digest}'
            if not pseudonym or any(c in pseudonym for c in '/\\\x00') or pseudonym in {'.','..'}:
                raise ValueError('Unsafe pseudonym')
            owner = (kind, canonical)
            if pseudonym.casefold() in owners and owners[pseudonym.casefold()] != owner:
                raise ValueError('Distinct identities cannot share a pseudonym')
            owners[pseudonym.casefold()] = owner
            for alias in [entry['identity'], *entry.get('aliases', [])]:
                if not alias.strip():
                    raise ValueError('Empty alias')
                previous = self.mapping.get(alias.casefold())
                if previous and previous != pseudonym:
                    raise ValueError('Ambiguous alias')
                self.mapping[alias.casefold()] = pseudonym
        # One pass prevents cascading replacements; boundaries protect material names.
        alternatives = sorted(self.mapping, key=len, reverse=True)
        self.pattern = re.compile(r'(?<!\w)(?:' + '|'.join(map(re.escape, alternatives)) + r')(?!\w)', re.I) if alternatives else None

    def replace(self, value: str) -> str:
        return self.pattern.sub(lambda match: self.mapping[match.group().casefold()], value) if self.pattern else value

    def path(self, value: str) -> str:
        from pathlib import PurePosixPath
        p = PurePosixPath(value)
        if p.is_absolute() or '..' in p.parts:
            raise ValueError('Unsafe input path')
        # Preserve extension exactly; path components are identities only if registered.
        parts = list(p.parts)
        if not parts:
            raise ValueError('Empty path')
        suffix = PurePosixPath(parts[-1]).suffix
        parts[-1] = parts[-1][:-len(suffix)] if suffix else parts[-1]
        parts = [self.replace(part) for part in parts]
        parts[-1] += suffix
        return '/'.join(parts)
