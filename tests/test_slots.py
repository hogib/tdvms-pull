"""The pool, and the two states a slot is in at once.

The campaign this replaces kept its plus-addresses by hand and drifted to four
naming schemes -- `+a1..a7`, `+e1..e4`, `+m1..m8` and a lone `+n1` -- with no
record of which belonged to which station and no way to tell an idle slot from
one that was never created.
"""
import pytest

from conftest import chunk, write
from tdvms.slots import Pool, pool


def test_a_pool_is_one_address_and_a_count():
    assert pool("you@gmail.com", 3) == ["you+a1@gmail.com", "you+a2@gmail.com",
                                        "you+a3@gmail.com"]


def test_a_plus_address_is_refused_as_the_base():
    """Extending `you+a1@x` gives `you+a1+a1@x`, which the portal accepts and no
    mail server delivers -- indistinguishable from a portal gone quiet."""
    with pytest.raises(ValueError, match="already a plus-address"):
        pool("you+a1@gmail.com", 3)


def test_a_non_address_is_refused():
    with pytest.raises(ValueError, match="not an email address"):
        pool("you", 3)


def test_an_empty_pool_is_refused():
    with pytest.raises(ValueError, match="at least one slot"):
        pool("you@gmail.com", 0)


def test_a_slot_knows_which_chunk_it_holds(ledger_path):
    led = write(ledger_path, [chunk("ELBA", state="submitted", email="you+a2@gmail.com")])
    p = Pool("you@gmail.com", 3).refresh(led)
    assert p.get("you+a2@gmail.com").holding["station"] == "ELBA"
    assert p.get("you+a1@gmail.com").free


def test_a_slot_matches_its_address_case_insensitively(ledger_path):
    """Mail servers do not agree on case and the portal echoes back whatever it
    was given, so a link delivered to `You+A3@...` must still name slot a3."""
    led = write(ledger_path, [chunk("ELBA", state="submitted", email="You+A3@Gmail.com")])
    p = Pool("you@gmail.com", 3).refresh(led)
    assert p.get("YOU+A3@GMAIL.COM").holding is not None
    assert p.get("you+a3@gmail.com").holding is not None


def test_a_terminal_chunk_does_not_hold_a_slot(ledger_path):
    led = write(ledger_path, [chunk("ELBA", state="nodata", email="you+a1@gmail.com")])
    p = Pool("you@gmail.com", 2).refresh(led)
    assert len(p.free()) == 2


def test_a_busy_slot_is_not_free_even_when_it_holds_nothing():
    """Local state said BLKS's slots were free after it was marked failed. The
    portal answered 111 to the next two submissions because it was still
    holding the abandoned requests."""
    p = Pool("you@gmail.com", 1)
    slot = p.get("you+a1@gmail.com")
    assert slot.free
    slot.mark_busy(1800)
    assert not slot.free and slot.remote_busy
    assert p.free() == []


def test_a_cooldown_of_zero_expires_immediately():
    p = Pool("you@gmail.com", 1)
    p.get("you+a1@gmail.com").mark_busy(0)
    assert p.get("you+a1@gmail.com").free
