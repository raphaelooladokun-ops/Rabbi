from core import reference
from core.proposals import propose_party


def test_state_lookup_from_text():
    assert reference.state_code_from_text("12 Allen Ave, Ikeja, Lagos") == "NG-LA"
    assert reference.state_code_from_text("somewhere in Kano state") == "NG-KN"
    assert reference.state_code_from_text("Abuja FCT") == "NG-FC"
    assert reference.state_code_from_text("no state here") == ""


def test_lga_lookup_and_state_backfill():
    code, state = reference.lga_from_text("Plot 5, Ikeja, Lagos")
    assert code == "NG-LA-IKE"
    assert state == "NG-LA"
    # Constraining to the wrong state yields nothing.
    assert reference.lga_from_text("Ikeja", state_code="NG-KN") == ("", "")


def test_invoice_type_reference_labels():
    types = reference.invoice_types()
    assert types["388"] == "Statement of Account"
    assert types["381"] == "Commercial Invoice"
    assert reference.invoice_type_label("388") == "Statement of Account"
    # Reverse lookup drives the default invoice_type_code (source of truth).
    assert reference.invoice_type_code_for("Commercial Invoice") == "381"


def test_default_invoice_type_code_is_commercial_invoice():
    from core.config import INVOICE_TYPE_CODE
    from core.masters import ClientConfig
    assert INVOICE_TYPE_CODE == "381"
    assert ClientConfig(id="x", name="X", reader="geeta").invoice_type_code == "381"


def test_hsn_and_service_validation():
    assert reference.is_valid_hsn("0101.21") is True
    assert reference.is_valid_hsn("010121") is True   # dots/format ignored
    assert reference.is_valid_hsn("9999.99") is False
    assert reference.is_valid_service_code("6201") is True  # computer programming
    assert reference.is_valid_service_code("0000") is False


def test_normalize_hsn_forces_xxxx_dot_xx():
    # pads short codes with trailing zeros, keeps the 6-digit subheading
    assert reference.normalize_hsn("9031.8") == "9031.80"
    assert reference.normalize_hsn("392020") == "3920.20"
    assert reference.normalize_hsn("0101.21") == "0101.21"
    assert reference.normalize_hsn("29096010") == "2909.60"  # 10-digit -> 6
    assert reference.normalize_hsn("101") == "1010.00"
    # blank stays blank — we never invent a code
    assert reference.normalize_hsn("") == ""
    assert reference.normalize_hsn("   ") == ""
    assert reference.normalize_hsn("N/A") == ""


def test_party_proposal_fills_lga_and_state_from_address():
    p = propose_party("Acme", tin_hint="01234567-0001", email="a@b.com",
                      address_text="14 Allen Avenue, Ikeja, Lagos")
    assert p.can_be_b2b is True
    assert p.state == "NG-LA"
    assert p.local_government == "NG-LA-IKE"
