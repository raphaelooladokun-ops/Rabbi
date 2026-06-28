from decimal import Decimal

from core.config import DEFAULT_TAX_RATES
from core.digitax_resources import TAX_CATEGORY_CODES, TAX_CATEGORY_RATES
from core.masters import MasterStore


def test_official_rate_bearing_categories():
    assert TAX_CATEGORY_RATES["STANDARD_VAT"] == Decimal("0.075")
    assert TAX_CATEGORY_RATES["REDUCED_VAT"] == Decimal("0.075")
    assert TAX_CATEGORY_RATES["ZERO_VAT"] == Decimal("0")
    assert TAX_CATEGORY_RATES["EXEMPTED"] == Decimal("0")
    # Non-VAT taxes carry no rate (must not silently become 0.075).
    assert "SERVICE_TAX" not in TAX_CATEGORY_RATES
    assert "STANDARD_VAT" in TAX_CATEGORY_CODES


def test_store_resolves_official_and_alias_categories(tmp_path):
    s = MasterStore(tmp_path / "c")
    assert s.tax_rate_for("STANDARD_VAT") == Decimal("0.075")
    assert s.tax_rate_for("ZERO_VAT") == Decimal("0")
    assert s.tax_rate_for("EXEMPTED") == Decimal("0")
    # Back-compat aliases still resolve.
    assert s.tax_rate_for("EXEMPT") == Decimal("0")
    # A non-rate-bearing category is unknown -> engine will flag it.
    assert s.tax_rate_for("WITHHOLDING_TAX") is None


def test_default_rates_include_official_set():
    for code in ("STANDARD_VAT", "ZERO_VAT", "EXEMPTED", "REDUCED_VAT"):
        assert code in DEFAULT_TAX_RATES
