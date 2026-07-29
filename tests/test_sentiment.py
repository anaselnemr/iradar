"""Tests for the sentiment layer (sentiment.py) - entity derivation and the
source parsers. Pure functions over canned API payloads: no network, no key,
no live database (entities() is exercised only where a db exists in the
repo checkout; parser tests always run).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "iradar"))
import sentiment as sn   # noqa: E402


# --- entity naming ----------------------------------------------------------

def test_family_title_cleaning():
    assert sn._clean_family("PSOC™ MCUs | Infineon Technologies") == "PSOC MCUs"
    assert sn._clean_family("Calypso® products | Infineon Technologies") == "Calypso products"
    assert sn._clean_family("  32-bit   XMC™  MCUs ") == "32-bit XMC MCUs"


def test_distinctive_token_becomes_bare_query():
    # distinctive brand names search bare; generic names get the qualifier
    tok = next((d for d in sn._DISTINCTIVE if d == "PSOC"), None)
    assert tok, "PSOC must be a distinctive brand token"
    assert "AURIX" in sn._DISTINCTIVE and "OPTIGA" in sn._DISTINCTIVE
    # the rule itself: a name with no distinctive token gets 'infineon ' prefix
    import re
    name = "Contactless memories"
    hit = next((d for d in sn._DISTINCTIVE
                if re.search(r"\b" + re.escape(d) + r"\b", name, re.I)), None)
    assert hit is None


# --- parsers ----------------------------------------------------------------

def test_parse_stackexchange():
    payload = {"items": [{
        "link": "https://electronics.stackexchange.com/questions/1",
        "title": "PSOC 6 deep sleep current too high",
        "owner": {"display_name": "eng_anna"},
        "creation_date": 1752800000, "score": 4, "answer_count": 2,
    }, {"link": "", "title": "no url, dropped"}]}
    ms = sn.parse_stackexchange(payload, "electronics")
    assert len(ms) == 1
    m = ms[0]
    assert m["title"].startswith("PSOC 6") and m["author"] == "eng_anna"
    assert "electronics" in m["extra"] and "2 answers" in m["extra"]
    assert m["posted"].startswith("2025") or m["posted"].startswith("2026")


def test_parse_hn_comment_and_story():
    payload = {"hits": [
        {"objectID": "41", "title": "Infineon buys a fab", "author": "pg",
         "created_at": "2026-07-01T10:00:00.000Z"},
        {"objectID": "42", "story_title": "Ask HN: MCU picks?",
         "comment_text": "The <b>AURIX</b> toolchain is honestly&nbsp;painful",
         "author": "dev2", "created_at": "2026-07-02T10:00:00.000Z"},
    ]}
    ms = sn.parse_hn(payload)
    assert len(ms) == 2
    assert ms[0]["url"].endswith("id=41")
    assert "<b>" not in ms[1]["body"], "html must be stripped from comments"
    assert "AURIX" in ms[1]["body"]


def test_parse_reddit():
    payload = {"data": {"children": [{"data": {
        "permalink": "/r/embedded/comments/abc/psoc_vs_stm32/",
        "title": "PSOC vs STM32 for a battery product?",
        "selftext": "Long text " * 100,
        "author": "u_dev", "created_utc": 1752800000,
        "subreddit": "embedded", "num_comments": 57,
    }}]}}
    ms = sn.parse_reddit(payload)
    assert len(ms) == 1
    m = ms[0]
    assert m["url"].startswith("https://www.reddit.com/r/embedded/")
    assert len(m["body"]) <= 400, "selftext must be capped"
    assert "r/embedded" in m["extra"] and "57 comments" in m["extra"]


def test_parsers_survive_garbage():
    assert sn.parse_stackexchange({}, "x") == []
    assert sn.parse_stackexchange(None, "x") == []
    assert sn.parse_hn({"hits": [{}]}) == []
    assert sn.parse_reddit({"data": {"children": [{"data": {}}]}}) == []


# --- the relevance gate -----------------------------------------------------

def test_relevance_gate_keeps_on_topic_and_drops_noise():
    # distinctive-token query: the token must appear in the stored text
    assert sn._relevant({"title": "PSOC 6 deep sleep current", "body": ""}, "PSOC")
    assert not sn._relevant({"title": "The J1 Forth CPU (2010)", "body": ""}, "PSOC")
    # 'infineon X' query: the text must actually say infineon. The old rule
    # accepted the family phrase alone - which filed "Reading a smart card
    # with a PN532" (an NXP part!) under Infineon's sentiment, and let
    # "my garden bias and control" pass for "infineon Bias".
    q = "infineon Smart card"
    assert sn._relevant({"title": "Infineon RSA key generation issue", "body": ""}, q)
    assert not sn._relevant({"title": "Reading a smart card with a PN532", "body": ""}, q)
    assert not sn._relevant({"title": "my garden bias and control", "body": ""}, "infineon Bias")
    assert not sn._relevant({"title": "Experiences building an OS in Rust", "body": ""}, q)
    # the body counts too (HN comments carry the substance)
    assert sn._relevant({"title": "Ask HN: MCU picks?",
                         "body": "the AURIX toolchain is painful"}, "AURIX")


def test_relevance_gate_word_boundaries_and_ambiguous_brands():
    # substring traps: an orchid genus is not the Calypso smart-card family
    assert not sn._relevant({"title": "Calypsoinae orchid germination", "body": ""}, "Calypso")
    # ambiguous brands need context: XMC the PCIe mezzanine standard is not
    # XMC the Infineon MCU family
    assert not sn._relevant({"title": "XMC mezzanine module for 10 GbE", "body": ""}, "XMC")
    assert sn._relevant({"title": "XMC4700 ADC jitter", "body": ""}, "XMC")
    assert sn._relevant({"title": "XMC dev board", "body": "an infineon eval kit"}, "XMC")
    assert sn._relevant({"title": "Calypso applet on a JavaCard", "body": ""}, "Calypso")
    # model-number suffixes still count as the word (psoc6, xmc4700)
    assert sn._relevant({"title": "psoc6 ble pairing", "body": ""}, "PSOC")


def test_parse_github_issue():
    payload = {"items": [{
        "html_url": "https://github.com/x/y/issues/9",
        "title": "AURIX TC375 lockstep trap",
        "body": "We hit CPU1  lockstep\ntraps when writing DFLASH " + "x" * 500,
        "user": {"login": "safetyeng"},
        "created_at": "2026-07-23T15:20:11Z",
        "state": "open", "comments": 5,
        "repository_url": "https://api.github.com/repos/x/y"}]}
    out = sn.parse_github(payload)
    assert len(out) == 1
    m = out[0]
    assert m["url"].endswith("/issues/9")
    assert m["author"] == "safetyeng"
    assert m["posted"] == "2026-07-23T15:20:11"
    assert "x/y" in m["extra"] and "5 comment(s)" in m["extra"]
    assert len(m["body"]) <= 400 and "\n" not in m["body"]


def test_parse_discourse_topic_with_blurb():
    payload = {
        "posts": [{"topic_id": 7, "blurb": "Took <b>ten</b> minutes to blink"}],
        "topics": [{"id": 7, "slug": "boot-kit", "title": "XMC1100 Boot Kit",
                    "created_at": "2026-07-24T18:40:00.351Z",
                    "posts_count": 5}]}
    out = sn.parse_discourse(payload, "forum.arduino.cc", "Arduino forum")
    assert out[0]["url"] == "https://forum.arduino.cc/t/boot-kit/7"
    assert out[0]["body"] == "Took ten minutes to blink"
    assert out[0]["posted"] == "2026-07-24T18:40:00"
    assert "4 repl(ies)" in out[0]["extra"]


def test_parse_wordpress_post():
    payload = [{"link": "https://hackaday.com/psoc/",
                "title": {"rendered": "A PSoC harp &#8217;s strings"},
                "excerpt": {"rendered": "<p>CapSense does the work&#8230;</p>"},
                "date": "2026-07-20T12:00:39"}]
    out = sn.parse_wordpress(payload, "Hackaday")
    assert out[0]["title"] == "A PSoC harp 's strings"
    assert out[0]["body"] == "CapSense does the work…"
    assert out[0]["extra"] == "Hackaday"


def test_new_parsers_survive_garbage():
    assert sn.parse_github(None) == []
    assert sn.parse_github({"items": [{}]}) == []
    assert sn.parse_discourse(None, "h", "l") == []
    assert sn.parse_wordpress(None, "l") == []
    assert sn.parse_wordpress([{}], "l") == []


def test_entity_for_prefers_distinctive_over_generic_lead():
    """The bug this pins: entity '32-bit FM Arm...' matched any title
    containing '32-bit', so AURIX pages filed under the FM family for as long
    as the community collector existed."""
    assert sn.entity_for("AURIX\u2122 TC3xx 32-bit | Infineon") == "AURIX"
    assert sn.entity_for("PSOC\u2122 4 MCUs | Infineon") == "PSOC"
    assert sn.entity_for("") == ""


def test_registry_is_consistent():
    keys = [s["key"] for s in sn.SOURCES]
    assert len(keys) == len(set(keys)), "duplicate source key"
    for s in sn.SOURCES:
        assert s["scope"] in ("entity", "global")
        assert s["label"] and callable(s["ready"])
        if s["scope"] == "entity":
            assert callable(s["collect"])


def test_parse_reddit_rss():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <author><name>/u/embedded_dev</name></author>
        <content type="html">&lt;p&gt;Anyone using the &lt;b&gt;TC375&lt;/b&gt;
          lite kit?&lt;/p&gt;</content>
        <link href="https://www.reddit.com/r/embedded/comments/abc/aurix/"/>
        <updated>2026-07-27T09:15:33+00:00</updated>
        <title>AURIX TC375 lite kit experiences?</title>
      </entry>
      <entry>
        <title>no link, must be dropped</title>
        <updated>2026-07-27T09:15:33+00:00</updated>
      </entry>
    </feed>"""
    out = sn.parse_reddit_rss(xml)
    assert len(out) == 1
    m = out[0]
    assert m["title"] == "AURIX TC375 lite kit experiences?"
    assert m["url"].endswith("/aurix/")
    assert m["author"] == "embedded_dev"
    assert m["posted"] == "2026-07-27T09:15:33"
    assert m["body"] == "Anyone using the TC375 lite kit?"
    assert sn.parse_reddit_rss("not xml at all") == []


def test_parse_innertube_finds_videos_wherever_they_sit():
    payload = {"contents": {"deep": [{"itemSectionRenderer": {"contents": [
        {"videoRenderer": {
            "videoId": "abc123",
            "title": {"runs": [{"text": "PSoC 6 "}, {"text": "deep dive"}]},
            "ownerText": {"runs": [{"text": "Infineon"}]},
            "publishedTimeText": {"simpleText": "2 weeks ago"},
            "detailedMetadataSnippets": [
                {"snippetText": {"runs": [{"text": "CapSense walkthrough"}]}}],
        }},
        {"somethingElse": {}},
    ]}}]}}
    out = sn.parse_innertube(payload)
    assert len(out) == 1
    m = out[0]
    assert m["url"] == "https://www.youtube.com/watch?v=abc123"
    assert m["title"] == "PSoC 6 deep dive"
    assert m["author"] == "Infineon"
    assert m["extra"] == "youtube | 2 weeks ago"
    assert m["body"] == "CapSense walkthrough"
    assert sn.parse_innertube(None) == []
    assert sn.parse_innertube({"a": [{"videoRenderer": {}}]}) == []


def test_aliases_and_query_repair():
    """The retrieval-side SOTA pass: heritage names and part prefixes are
    searched as their own queries, same-query families collapse to one, and
    a two-word cut never ends on a joining word."""
    # alias hits pass the gate under the query that found them
    assert sn._relevant({"title": "CY8C4247 wont enter deep sleep",
                         "body": ""}, "CY8C")
    assert sn._relevant({"title": "Pi Pico W cyw43439 driver panic",
                         "body": ""}, "CYW43")
    assert sn._relevant({"title": "Moving from Cypress PSoC to ModusToolbox",
                         "body": ""}, "Cypress PSoC")
    # the alias table rides along in entities() output
    assert "CY8C" in sn._ALIASES["PSOC"]
    assert "CYW43" in sn._ALIASES["AIROC"]
    # trailing joining words are cut, not searched
    assert "and" in sn._QUERY_STOP and "for" in sn._QUERY_STOP


def test_negative_context_rejects_the_other_psoc():
    """'PSOC' in a health thread is a typo of PCOS, not a microcontroller.
    The give-away words are always right there, so the gate carries negative
    context per token - and the URL counts as evidence, because the
    subreddit is in it."""
    assert not sn._relevant({"title": "My PSOC diagnosis journey",
                             "body": "metformin and hormones",
                             "url": "https://reddit.com/r/PCOS/x"}, "PSOC")
    assert not sn._relevant({"title": "psoc flare and insulin resistance",
                             "body": "", "url": ""}, "PSOC")
    # the URL alone is enough
    assert not sn._relevant({"title": "psoc rant", "body": "",
                             "url": "https://www.reddit.com/r/PCOSloseit/1"},
                            "PSOC")
    # and the real thing still passes clean
    assert sn._relevant({"title": "PSoC 6 CapSense tuning", "body": "",
                         "url": "https://community.infineon.com/x"}, "PSOC")
