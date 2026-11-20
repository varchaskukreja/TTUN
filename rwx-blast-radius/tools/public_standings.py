#!/usr/bin/env python3
"""Read the public track standings without a browser, login, or cookies."""
import json
import re
import subprocess
from datetime import datetime, timezone
from html.parser import HTMLParser

URL = 'https://www.joinbuilders.org/buildathon/tracks/blast-radius'


class StandingsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.row = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'ol' and attrs.get('aria-label') == 'Standings, RWX - Blast Radius':
            self.active = True
        elif tag == 'li' and self.active:
            self.row = []

    def handle_data(self, value):
        if self.row is not None:
            self.row.append(value)

    def handle_endtag(self, tag):
        if tag == 'li' and self.row is not None:
            value = ' '.join(' '.join(self.row).split())
            match = re.fullmatch(r'(\d+|—) (.+) ([\d,.]+) s', value)
            if not match:
                raise ValueError(f'Unrecognized standings row: {value!r}')
            self.rows.append({'rank': None if match[1] == '—' else int(match[1]),
                              'team': match[2], 'seconds': float(match[3].replace(',', ''))})
            self.row = None
        if tag == 'ol':
            self.active = False


def main():
    response = subprocess.run(['curl', '--fail', '--silent', '--show-error',
                               '--location', '--max-time', '30', URL],
                              check=True, capture_output=True, text=True)
    parser = StandingsParser()
    parser.feed(response.stdout)
    if not parser.rows:
        raise SystemExit('Public standings unavailable; refusing to infer rank.')
    print(json.dumps({'observed_at': datetime.now(timezone.utc).isoformat(),
                      'url': URL, 'standings': sorted(parser.rows, key=lambda r: r['rank'] or 0)}, indent=2))


if __name__ == '__main__':
    main()
