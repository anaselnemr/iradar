"""Offline browser regression for sentiment browsing; uses only a temporary DB.

Run: .venv/Scripts/python.exe qa/sentiment_browse.py
"""
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import test_routes as fixture
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


def main():
    fixture._seed_mentions()
    output = Path(tempfile.mkdtemp(prefix='iradar_sentiment_qa_'))
    server = make_server('127.0.0.1', 0, fixture.app.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:%d' % server.server_port
    try:
        with fixture._pagination_mentions(), sync_playwright() as pw:
            con = fixture.db.connect()
            con.execute("UPDATE mentions SET fulltext=?, topics='docs-', score=-.7, reason='Missing setup steps' WHERE url='https://pagination.invalid/0'",
                        ('Infineon documentation omits important setup steps. ' * 30,))
            con.execute("UPDATE mentions SET topics='hardware+', reason='Works reliably' WHERE url='https://pagination.invalid/1'")
            con.execute("INSERT INTO mentions(entity,source,url,title,body) VALUES('PSOC','blog','https://pagination.invalid/noise','Merus wine tasting','Cabernet and wine reviews')")
            con.commit()
            con.close()
            browser = pw.chromium.launch(args=['--disable-gpu',
                '--disable-features=CalculateNativeWinOcclusion',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding'])
            ctx = browser.new_context(viewport={'width': 1440, 'height': 1000})
            ctx.add_cookies([{'name': 'iradar_session',
                             'value': fixture._client.get_cookie('iradar_session').value,
                             'url': url}])
            page = ctx.new_page()
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(url + '/sentiment')
            assert page.locator('.fam').count() == 12
            assert page.locator('#snresults .mn > li').count() == 50
            bounds = page.locator('#snform').bounding_box()
            assert bounds['height'] < 70, bounds
            desktop_mentions_top = page.locator('#snresults').evaluate('(el) => el.offsetTop')
            assert desktop_mentions_top < 1700, desktop_mentions_top
            page.screenshot(path=str(output / 'desktop.png'), full_page=False)
            page.locator('#snfamilies').screenshot(path=str(output / 'families.png'))
            assert page.locator('#sninsights, .terms, .mn-more, .mn-full').count() == 0
            first = page.locator('.fam').first.get_attribute('data-entity')
            page.locator('#snfamilies a[aria-label="Next page"]').click()
            assert page.locator('.fam').first.get_attribute('data-entity') != first
            page.bring_to_front()
            page.locator('#snfamilies .pgap').fill('45')
            page.locator('#snfamilies .pgap').press('Enter')
            page.wait_for_url('**/*family_page=45*')
            assert page.locator('#snfamilies [aria-current=page]').inner_text() == '45'
            page.locator('#snresults a[aria-label="Next page"]').first.click()
            assert 'page=2' in page.url
            page.locator('#snq').fill('Pagination fixture 539')
            page.locator('#snsearch h3').click()  # blur must not cancel live search
            page.wait_for_function("document.querySelectorAll('.fam').length === 1")
            assert page.locator('#snresults .mn > li').count() == 1
            assert 'family_page=' not in page.url and '?page=' not in page.url
            page.locator('.fam').first.focus()
            page.keyboard.press('Enter')
            page.locator('.fam-panel .mn > li').first.wait_for()
            assert page.locator('.fam.open').count() == 1
            page.locator('.fam-close').click()
            assert page.locator('.fam.open').count() == 0
            page.locator('#snq').fill('no-matching-fixture')
            page.locator('#snresults .sn-empty').wait_for()
            page.locator('#snq').press('Escape')
            page.wait_for_function("document.querySelectorAll('.fam').length === 12")
            page.locator('#snform summary').filter(has_text='Product line').click()
            page.locator('input[name=pl][value="81"]').check()
            page.locator('#snbtn').click()
            page.wait_for_url('**/sentiment?pl=81')
            assert page.locator('.fam').count() == 1
            assert page.locator('.fam').first.get_attribute('data-entity') == 'OPTIGA'
            page.locator('#snreset').click()
            page.locator('#snq').fill('PSOC')
            page.wait_for_function("document.querySelectorAll('.fam').length === 1")
            page.locator('#snform summary').filter(has_text='Period').click()
            page.locator('input[name=period][value="30d"]').check()
            page.locator('#snbtn').click()
            page.wait_for_url('**/*period=30d*')
            page.locator('.fam').first.click()
            page.locator('.fam-panel .mn > li').first.wait_for()
            assert page.locator('.fam-panel .mn > li').count() == 1, 'drawer lost period filter'
            page.locator('#snreset').click()
            page.locator('#snform summary').filter(has_text='Period').click()
            page.locator('input[name=period][value=custom]').check()
            page.locator('input[name=from]').fill('2026-09-01')
            page.locator('input[name=to]').fill('2026-09-30')
            page.locator('#snbtn').click()
            page.wait_for_url('**/*from=2026-09-01*')
            page.locator('#snform summary').filter(has_text='Period').click()
            page.locator('input[name=period][value=""]').check()
            assert page.locator('input[name=from]').input_value() == ''
            page.locator('#snbtn').click()
            page.wait_for_url(url + '/sentiment')
            # Failed requests must be visible, and the same Apply must retry.
            page.route('**/sentiment?*', lambda route: route.fulfill(status=503, body='Unavailable'))
            page.locator('#snq').fill('Pagination fixture 539')
            page.wait_for_function("document.getElementById('snfeedback').textContent.includes('Could not')")
            page.unroute('**/sentiment?*')
            page.locator('#snbtn').click()
            page.wait_for_function("document.querySelectorAll('.fam').length === 1")
            page.goto(url + '/sentiment')
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile overflow'
            mobile_mentions_top = page.locator('#snresults').evaluate('(el) => el.offsetTop')
            assert mobile_mentions_top < 3200, mobile_mentions_top
            page.screenshot(path=str(output / 'mobile.png'))
            page.locator('#snform summary').filter(has_text='Product line').click()
            panel = page.locator('#snform details[open] .panel').bounding_box()
            assert panel['x'] >= 0 and panel['x'] + panel['width'] <= 390
            page.screenshot(path=str(output / 'mobile-filter.png'))
            page.locator('input[name=pl][value="81"]').check()
            page.locator('#snbtn').click()
            page.wait_for_url('**/sentiment?pl=81')
            page.locator('#snresults').scroll_into_view_if_needed()
            page.screenshot(path=str(output / 'mobile-results.png'))
            page.goto(url + '/sentiment?q=Pagination+fixture+000')
            assert 'important setup steps' in page.locator('#snresults .mn-body').inner_text()
            assert page.locator('#sninsights, .terms, .mn-more, .mn-full').count() == 0
            page.locator('#snresults').screenshot(path=str(output / 'mobile-evidence.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.goto(url + '/sentiment/excluded')
            assert 'Merus wine tasting' in page.locator('main').inner_text()
            page.goto(url + '/sentiment?q=Pagination+fixture+000')
            assert page.locator('#snresults .mn > li').count() == 1
            assert not errors, errors
            print(json.dumps({'url': url + '/sentiment', 'screenshots': str(output),
                              'desktop_mentions_top': desktop_mentions_top,
                              'mobile_mentions_top': mobile_mentions_top,
                              'browser_errors': errors, 'result': 'PASS'}))
            browser.close()
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
