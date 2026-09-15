"""Company relevance and evidence-linked summaries. No network or model calls."""
import re
from functools import lru_cache
from urllib.parse import urlsplit

# Generic product categories and the assigned family are NOT proof of ownership.
BRANDS = ('psoc', 'aurix', 'optiga', 'airoc', 'traveo', 'xensiv', 'coolmos',
          'coolgan', 'coolsic', 'easypim', 'hybridpack', 'modustoolbox', 'secora',
          'imotion', 'eicedriver', 'optimos', 'strongirfet', 'motix', 'profet',
          'litix', 'hitfet', 'coolset', 'trenchstop', 'cipos', 'ez-usb', 'ez-pd',
          'optireg', 'easypack', 'econopack', 'econodual', 'cipurse')
_BRAND = re.compile(r'(?<![a-z0-9])(?:' + '|'.join(BRANDS) + r')(?![a-z])', re.I)
_TECH = re.compile(r'\b(?:infineon|semiconductor|chip|mcu|microcontroller|firmware|'
                   r'amplifier|mosfet|smartcard|smart card|javacard|nfc|transistor|wiced|dave|pwm|gpio)\b', re.I)
_AMBIGUOUS = re.compile(r'\b(?:cypress|calypso|merus|tricore|xmc\d*)\b', re.I)
_PART = re.compile(r'\b(?:CY8C\d+[a-z0-9-]*|CYW\d{3,}[a-z0-9-]*|XMC[14]\d{3}[a-z0-9-]*)\b', re.I)
_HEALTH = re.compile(r'\b(?:pcos|ovarian|ovaries|metformin|menstrual|fertility|insulin|diagnosis)\b', re.I)


@lru_cache(maxsize=8192)
def company_related(title, body, fulltext, url):
    """Conservative evidence gate, including already-stored mentions."""
    text = ' '.join((title or '', body or '', fulltext or ''))
    try:
        host = (urlsplit(url or '').hostname or '').lower()
    except ValueError:
        host = ''
    if host == 'infineon.com' or host.endswith('.infineon.com'):
        return 1
    if re.search(r'\binfineon\b', text, re.I):
        return 1
    if (_HEALTH.search(text + ' ' + (url or '')) or re.search(r'/r/pcos[^/]*(?:/|$)', url or '', re.I)) and not _TECH.search(text):
        return 0
    return int(bool(_BRAND.search(text) or _PART.search(text)
                    or (_AMBIGUOUS.search(text) and _TECH.search(text))))


def insights(con, base, args):
    """One URL, one piece of evidence. All current filters apply.

    Deterministic, auditable synthesis rather than fabricated model prose.
    Never call a volume increase an emerging issue without a comparison.
    """
    rows = con.execute('SELECT id,url,title,entity,source,score,topics,reason,posted,'
                       'fetched,fulltext,body,read_state,text_checked_at,scored_at '
                       + base + ' ORDER BY id ASC', args).fetchall()
    unique = {}
    for row in rows:
        unique.setdefault(row['url'], dict(row))
    rows = list(unique.values())
    scored = [r for r in rows if r['score'] is not None]
    positives = sorted((r for r in scored if r['score'] > .15), key=lambda r: -r['score'])
    negatives = sorted((r for r in scored if r['score'] < -.15), key=lambda r: r['score'])
    # Attribute themes to the posts that supply them, never count a repeated
    # family assignment twice. Vendor posts stay visibly labelled as such.
    def themes(sign):
        groups = {}
        for r in scored:
            tags = set((r['topics'] or '').split(','))
            if 'vendor' in tags:
                continue
            for tag in tags:
                if tag.endswith(sign):
                    groups.setdefault(tag[:-1], []).append(r)
        return [{'name': name, 'count': len(evidence),
                 'evidence': sorted(evidence, key=lambda r: r['posted'] or r['fetched'], reverse=True)[:2]}
                for name, evidence in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0]))[:3]]
    return dict(total=len(rows), scored=len(scored), positive=len(positives),
                negative=len(negatives), neutral=len(scored)-len(positives)-len(negatives),
                concerns=themes('-'), strengths=themes('+'),
                recent=sorted(rows, key=lambda r: r['posted'] or r['fetched'], reverse=True)[:3],
                text=sum(bool(r['fulltext']) for r in rows),
                snippets=sum(bool(r['body']) and not r['fulltext'] for r in rows),
                unread=sum(not (r['fulltext'] or r['body']) for r in rows),
                failed=sum(r['read_state'] in ('unavailable', 'unsupported') for r in rows),
                undated=sum(not r['posted'] for r in rows),
                avg=sum(r['score'] for r in scored)/len(scored) if scored else None)


def coverage(con):
    return [dict(r) for r in con.execute('''
        SELECT source,COUNT(DISTINCT url) total,
          COUNT(DISTINCT CASE WHEN score IS NOT NULL THEN url END) scored,
          COUNT(DISTINCT CASE WHEN fulltext<>'' THEN url END) readable,
          COUNT(DISTINCT CASE WHEN read_state IN ('unavailable','unsupported') THEN url END) unavailable,
          COUNT(DISTINCT CASE WHEN text_checked_at<>'' AND
            text_checked_at>=datetime('now','-14 days') THEN url END) fresh,
          MAX(text_checked_at) checked
        FROM sentiment_mentions GROUP BY source ORDER BY total DESC
    ''')]
