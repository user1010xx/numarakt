"""phone_utils birim testleri."""

from phone_utils import normalize_tr_phone, phone_match_keys, phones_equal


def test_normalize_common_tr_formats():
    assert normalize_tr_phone("905466033161") == "905466033161"
    assert normalize_tr_phone("+90 546 603 31 61") == "905466033161"
    assert normalize_tr_phone("05466033161") == "905466033161"
    assert normalize_tr_phone("5466033161") == "905466033161"


def test_normalize_rejects_extension():
    assert normalize_tr_phone("605") is None
    assert normalize_tr_phone("") is None
    assert normalize_tr_phone(None) is None


def test_phones_equal_across_formats():
    assert phones_equal("905466033161", "05466033161")
    assert phones_equal("+90 546 603 31 61", "5466033161")
    assert not phones_equal("605", "905466033161")


def test_phone_match_keys_include_last10():
    keys = phone_match_keys("905466033161")
    assert "905466033161" in keys
    assert "5466033161" in keys
