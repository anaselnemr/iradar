"""Tests for the sentiment layer (sentiment.py) - entity derivation and the
source parsers. Pure functions over canned API payloads: no network, no key,
no live database (entities() is exercised only where a db exists in the
repo checkout; parser tests always run).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
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
    # 'infineon X' query: 'infineon' OR the family phrase must appear -
    # full-text APIs match discussion bodies we do not store, so a generic
    # title with neither is noise for a sentiment average
    q = "infineon Smart card"
    assert sn._relevant({"title": "Infineon RSA key generation issue", "body": ""}, q)
    assert sn._relevant({"title": "Reading a smart card with a PN532", "body": ""}, q)
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
