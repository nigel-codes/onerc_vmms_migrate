"""Rewrite only quoted local attachment links inside historical HTML."""
import html
import re

LINK = re.compile(r'''(?P<prefix>\b(?:src|href)\s*=\s*)(?P<quote>["'])(?P<url>/(?:private/)?files/[^"']+)(?P=quote)''', re.I)


def rewrite(value, resolve):
    def replacement(match):
        url=html.unescape(match['url'])
        target=resolve(url)
        if not target:return match[0]
        return match['prefix']+match['quote']+html.escape(target,quote=True)+match['quote']
    return LINK.sub(replacement,value or '')
