"""Tests for oui_lookup and its integration into vendor_id.classify()."""

from __future__ import annotations

from oui_lookup import is_locally_administered, lookup_oui
from vendor_id import classify


def test_is_locally_administered_true_for_randomized_mac():
    # 0x02 bit set in the first octet -- e.g. "12" = 0b00010010.
    assert is_locally_administered("12:34:56:78:9a:bc") is True


def test_is_locally_administered_false_for_real_ieee_mac():
    # A real Cisco OUI (00 = 0b00000000, U/L bit clear).
    assert is_locally_administered("e8:0a:b9:11:22:33") is False


def test_lookup_oui_finds_real_registered_prefix():
    # E8:0A:B9 is Cisco Systems in the bundled IEEE database (see
    # data/oui.csv) -- a real, currently-registered assignment, not a
    # fabricated test fixture, so this also catches the CSV failing to load.
    assert lookup_oui("e8:0a:b9:11:22:33") == "Cisco Systems, Inc"


def test_lookup_oui_returns_none_for_locally_administered_mac():
    """Even if a randomized MAC's prefix happens to coincide with a real
    registered OUI (extremely unlikely, but the U/L bit differs), it must
    not be looked up -- the U/L bit alone proves this prefix was never
    IEEE-assigned to anyone.
    """
    assert lookup_oui("12:34:56:78:9a:bc") is None


def test_lookup_oui_returns_none_for_unassigned_prefix():
    # FF:FF:FF (the broadcast address's prefix) has never been an actual
    # IEEE OUI assignment.
    assert lookup_oui("ff:ff:ff:11:22:33") is None


def test_classify_falls_back_to_oui_when_no_fingerprint_matches():
    guess = classify("e8:0a:b9:11:22:33", device_name=None, adv_data_json=None)
    assert guess == {"vendor": "Cisco Systems, Inc", "confidence": "low", "reason": "oui"}


def test_classify_prefers_specific_fingerprint_over_oui():
    """A Garmin manufacturer-id match must win even if the MAC's OUI (if
    IEEE-registered) would also resolve to something -- a specific
    product-family match is more useful than a generic hardware vendor.
    """
    adv_data_json = '{"manufacturer_data": {"135": "00"}, "service_uuids": []}'
    guess = classify("e8:0a:b9:11:22:33", device_name=None, adv_data_json=adv_data_json)
    assert guess == {"vendor": "Garmin", "confidence": "high", "reason": "manufacturer_id"}


def test_classify_returns_none_for_randomized_mac_with_no_other_signal():
    guess = classify("12:34:56:78:9a:bc", device_name=None, adv_data_json=None)
    assert guess is None
