"""One message in, one event out. The classifier decides nothing else.

Splitting "what the mail says" from "what to do about it" is the rewrite. The
tool this replaces acted on a message by shelling out to its own CLI twice --
once to record the outcome, once to refill the slot -- so a successful first
call and a failed second left the ledger recording an answer for a slot nobody
filled, with nothing owning the pair.
"""
import email.message

import pytest

from conftest import message
from tdvms.mailbox import (Foreign, Link, NoData, Unrelated, classify,
                           recipient_of)

MINE = {"you+a1@gmail.com", "you+a2@gmail.com"}
URL = "https://tdvms.afad.gov.tr/files/abc123.zip"


def test_a_link_is_pulled_out_of_a_plain_text_mail():
    e = classify(b"1", message("you+a1@gmail.com", f"Indirme linki: {URL}"), MINE)
    assert isinstance(e, Link) and e.urls == [URL]
    assert e.to == "you+a1@gmail.com"


def test_a_link_in_an_html_part_is_found_too():
    """These arrive as HTML often enough that reading only text/plain sees an
    empty message and reports no link at all."""
    m = email.message.EmailMessage()
    m["To"], m["From"] = "you+a1@gmail.com", "noreply@tdvms.afad.gov.tr"
    m.set_content("nothing here")
    m.add_alternative(f'<html><body><a href="{URL}">indir</a></body></html>',
                      subtype="html")
    e = classify(b"1", m, MINE)
    assert isinstance(e, Link) and URL in e.urls


def test_the_same_link_twice_is_one_link():
    body = f"{URL}\nve tekrar: {URL}"
    e = classify(b"1", message("you+a1@gmail.com", body), MINE)
    assert e.urls == [URL]


def test_the_no_data_mail_carries_no_link_and_is_still_an_answer():
    """A real outcome with nothing to download. The address names the window,
    because a slot holds exactly one chunk at a time."""
    e = classify(b"2", message(
        "you+a2@gmail.com",
        "Talep ettiginiz istasyon/istasyonlara ait veri bulunmamaktadir."), MINE)
    assert isinstance(e, NoData) and e.to == "you+a2@gmail.com"


def test_a_link_for_an_address_we_never_used_is_foreign():
    """One mailbox can serve several campaigns and every poller runs the same
    search. Consuming another ledger's link makes its owner wait forever."""
    e = classify(b"3", message("other+z9@gmail.com", URL), MINE)
    assert isinstance(e, Foreign)


def test_claim_unknown_overrides_that_when_the_operator_insists():
    e = classify(b"3", message("other+z9@gmail.com", URL), MINE, claim_unknown=True)
    assert isinstance(e, Link)


def test_ordinary_mail_is_left_alone():
    """Marking it read to avoid re-scanning would consume somebody's real inbox."""
    e = classify(b"4", message("you+a1@gmail.com", "lunch?", sender="friend@x.com"), MINE)
    assert isinstance(e, Unrelated)


def test_delivered_to_wins_when_the_to_header_was_rewritten():
    """Forwarding setups rewrite `To`; without these headers a forwarded link
    names no slot and refills nothing."""
    m = email.message.EmailMessage()
    m["To"] = "shared@gmail.com"
    m["Delivered-To"] = "you+a1@gmail.com"
    m["From"] = "noreply@tdvms.afad.gov.tr"
    m.set_content(URL)
    assert recipient_of(m) == "shared@gmail.com"      # To is checked first
    del m["To"]
    assert recipient_of(m) == "you+a1@gmail.com"
