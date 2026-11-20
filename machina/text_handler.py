"""Known-identity replacement in XML values and text sidecars, never XML keys."""
import html
import re
import xml.etree.ElementTree as ET


def transform_xml(data, replace):
    # UTF-8 only: reject rather than corrupt other encodings.
    text=data.decode('utf-8-sig')
    ET.fromstring(text)
    token=re.compile(r'''<!\[CDATA\[.*?\]\]>|<!--.*?-->|<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>|[^<]+''',re.S)
    attrs=re.compile(r'''(=\s*)(["'])(.*?)\2''',re.S)
    changes=0
    def edit(raw, attribute=False):
        nonlocal changes
        value=html.unescape(raw);updated=replace(value)
        if updated==value:return raw
        changes+=1
        return html.escape(updated,quote=attribute)
    def part(m):
        nonlocal changes
        value=m.group()
        if value.startswith('<![CDATA['):
            updated=replace(value[9:-3])
            if ']]>' in updated:raise ValueError('Invalid CDATA replacement')
            changes+=updated!=value[9:-3]
            return '<![CDATA['+updated+']]>'
        if value.startswith('<!--'):
            updated=replace(value[4:-3])
            if '--' in updated:raise ValueError('Invalid XML comment replacement')
            changes+=updated!=value[4:-3]
            return '<!--'+updated+'-->'
        if value.startswith('<?') or value.startswith('<!'):return value
        if value.startswith('<'):
            return attrs.sub(lambda a:a[1]+a[2]+edit(a[3],True)+a[2],value)
        return edit(value)
    output=token.sub(part,text).encode('utf-8')
    if data.startswith(b'\xef\xbb\xbf'):output=b'\xef\xbb\xbf'+output
    ET.fromstring(output)
    return output,{'status':'processed','changes':changes,'limitations':['known_xml_values_only']}
