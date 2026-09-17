"""Content/relevance regressions, isolated SQLite and mocked source APIs."""
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import test_routes as routes  # its disposable database; never the project data
import db
import sentiment as sn
import sentiment_review as review
import mention_text as mt


@contextmanager
def mentions(rows):
    con = db.connect()
    ids = []
    for r in rows:
        values = dict(entity='PSOC', source='blog', title='Infineon test evidence',
                      body='Infineon test body', url='https://content-test.invalid/%d' % len(ids),
                      posted='2026-09-15 12:00:00', fetched=db.now_iso())
        values.update(r)
        ids.append(con.execute('INSERT INTO mentions (' + ','.join(values) + ') VALUES ('
                               + ','.join('?' for _ in values) + ')', list(values.values())).lastrowid)
    con.commit()
    try:
        yield con, ids
    finally:
        con.execute('DELETE FROM mentions WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids)
        con.commit()
        con.close()


def test_company_gate_requires_evidence_not_an_assigned_family():
    cases = [('Calypso beach music', '', '', 'https://example.org', 0),
             ('Garden sensor tutorial', '', '', 'https://infineon.com.evil.test', 0),
             ('PSoC timer period adjustment', '', '', '', 1),
             ('PSOC insulin resistance', '', '', 'https://reddit.com/r/PCOS', 0),
             ('Cypress tree pruning', '', '', '', 0),
             ('Cypress MCU firmware', '', '', '', 1),
             ('Upgrade Aurix platform with gold-backed trading and vault features', '', '', '', 0),
             ('PSOC Important Questions | Power system operation and control | Semester Exam', '', '', '', 0),
             ('AURIX TC375 lockstep trap', '', '', '', 1),
             ('New device', '', 'Infineon CoolSiC works well', '', 1),
             ('Solved question', '', '', 'https://community.infineon.com/t5/x', 1),
             ('CY8C4247 deep sleep', '', '', '', 1)]
    for *args, expected in cases:
        assert review.company_related(*args) == expected, args
    with mentions([dict(title='Wine review', body='Merus Cabernet', entity='PSOC')]) as (con, ids):
        assert con.execute('SELECT COUNT(*) FROM sentiment_mentions WHERE id=?', ids).fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM mentions WHERE id=?', ids).fetchone()[0] == 1


def test_hiding_and_restoring_is_reversible_and_applies_to_all_analysis_queries():
    with mentions([{}]) as (con, ids):
        mid = ids[0]
        token = routes._csrf()
        assert routes._client.post('/sentiment/relevance', data={'id':mid, 'action':'exclude', '_csrf':token}).status_code == 302
        assert con.execute('SELECT COUNT(*) FROM sentiment_mentions WHERE id=?', ids).fetchone()[0] == 0
        assert b'Infineon test evidence' in routes._ok('/sentiment/excluded').data
        assert b'Infineon test evidence' not in routes._ok('/sentiment?q=Infineon+test+evidence').data.split(b'id="snresults"',1)[1]
        assert b'Infineon test evidence' not in routes._ok('/sentiment/rows?entity=PSOC').data
        assert routes._client.post('/sentiment/relevance', data={'id':mid, 'action':'restore', '_csrf':token}).status_code == 302
        assert con.execute('SELECT COUNT(*) FROM sentiment_mentions WHERE id=?', ids).fetchone()[0] == 1


def test_summary_deduplicates_and_links_to_real_filtered_evidence():
    with mentions([dict(score=-.8, topics='docs-', reason='Documentation missing'),
                   dict(entity='AURIX', url='https://content-test.invalid/0', score=-.8, topics='docs-'),
                   dict(score=.7, topics='hardware+')]) as (con, ids):
        out = review.insights(con, 'FROM sentiment_mentions WHERE url LIKE ?', ['https://content-test.invalid/%'])
        assert out['total'] == 2 and out['scored'] == 2
        assert out['negative'] == 1 and out['positive'] == 1
        assert out['concerns'][0]['count'] == 1
        assert out['concerns'][0]['evidence'][0]['url'] == 'https://content-test.invalid/0'
        only = review.insights(con, 'FROM sentiment_mentions WHERE id=?', [ids[2]])
        assert only['total'] == 1 and not only['concerns']


def test_refresh_invalidates_stale_scores_and_preserves_originals_on_failure():
    with mentions([dict(fulltext='Old Infineon evidence', score=-.5, topics='docs-',
                        reason='Old reason', scored_at='2026-01-01 00:00:00')]) as (con, ids):
        with patch.object(mt, 'fetch_one', return_value=('New Infineon evidence', 'SOLVED', '', '')):
            assert sn.enrich_pending(limit=10, con=con) == 1
        row = con.execute('SELECT * FROM mentions WHERE id=?', ids).fetchone()
        assert row['fulltext'] == 'New Infineon evidence' and row['score'] is None
        assert row['topics'] == '' and row['reason'] == '' and row['scored_at'] == ''
        assert row['needs_rescore'] == 1
        assert row['read_state'] == 'read' and row['text_checked_at']
        with patch.object(mt, 'fetch_one') as fetch:
            sn.enrich_pending(limit=10, con=con)
            fetch.assert_not_called()
        con.execute("UPDATE mentions SET text_checked_at='2020-01-01',read_attempted_at='2020-01-01',score=.4 WHERE id=?", ids)
        con.commit()
        with patch.object(mt, 'fetch_one', return_value=None):
            sn.enrich_pending(limit=10, con=con)
        row = con.execute('SELECT * FROM mentions WHERE id=?', ids).fetchone()
        assert row['fulltext'] == 'New Infineon evidence' and row['score'] == .4
        assert row['read_state'] == 'unavailable'


def test_recent_community_collection_reads_beyond_the_page_widgets():
    def get(url, *args):
        return json.dumps({'data': {'items': [dict(subject='Infineon question',
             view_href='https://community.infineon.com/t5/topic/%d' % n,
             body='<p>Product question</p>', author={'login':'engineer'}) for n in range(100)]}})
    with patch.object(mt, 'get', side_effect=get) as fetch, patch.object(sn.time, 'sleep'):
        rows = sn.collect_community_recent()
    assert len(rows) == 300 and fetch.call_count == 3
    assert 'OFFSET%20200' in fetch.call_args[0][0]
    assert rows[0]['body'] == 'Product question'


def test_community_answers_are_separated_from_the_opening_post():
    def get(url, *args):
        items = ([dict(id='2', topic={'id':'1'}, body='<p>Use I2C, not SWD.</p>',
                       author={'login':'support'}, is_solution=True)] if 'topic.id' in url else
                 [dict(id='1', body='<p>PSoC programming failed.</p>', conversation={'solved':True}, replies={'count':1})])
        return json.dumps({'data': {'items':items}})
    with patch.object(mt, 'get', side_effect=get):
        text, ctx, _, _ = mt.community_batch(['1'])['1']
    assert '[Original post]' in text and 'Accepted answer — support' in text
    assert 'Use I2C, not SWD.' in text and 'SOLVED' in ctx


def test_model_cannot_score_an_excluded_or_out_of_batch_id():
    with mentions([{}, dict(title='Wine', body='Merus winery')]) as (con, ids):
        payload = json.dumps([{'id':mid, 'score':.5, 'topics':['hardware+'], 'why':'Works well'} for mid in ids])
        msg = SimpleNamespace(stop_reason='end_turn', usage=SimpleNamespace(input_tokens=1,output_tokens=1),
                              content=[SimpleNamespace(type='text',text=payload)])
        @contextmanager
        def stream(**kwargs):
            assert 'Never turn a product complaint' in kwargs['messages'][0]['content']
            yield SimpleNamespace(get_final_message=lambda:msg)
        client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
        with patch.object(sn, '_client', return_value=client):
            assert sn.score_pending(limit=100) == 1
        assert con.execute('SELECT score FROM mentions WHERE id=?', [ids[1]]).fetchone()[0] is None
        assert con.execute('SELECT scored_at FROM mentions WHERE id=?', [ids[0]]).fetchone()[0]


def test_clean_feed_keeps_searchable_evidence_without_removed_panels():
    with mentions([dict(fulltext='Infineon <script>bad()</script> ' + 'long evidence ' * 40)]):
        body = routes._ok('/sentiment?q=long+evidence').data.decode()
        assert '&lt;script&gt;' in body and '<script>bad()' not in body
        assert 'class="mn-body"' in body and 'long evidence' in body
        for removed in ('Read collected text', 'mn-full', 'What people are saying',
                        'What is searched', 'Evidence &amp; coverage', 'sninsights'):
            assert removed not in body, removed
