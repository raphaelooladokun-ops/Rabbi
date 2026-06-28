"""Authoritative Digitax/FIRS reference data.

Currently the tax-category vocabulary (name, code, VAT rate). Sourced from the
Digitax "Tax Categories" resource export so item `tax_category_code` values and
their rates match exactly what Digitax accepts — operators pick from these
codes, and the engine derives the VAT rate from the rate-bearing ones.

(The LGA code reference — LGA name -> NG-XX-XXX — is NOT in this file and is
still needed to auto-fill a party's local_government.)
"""
from __future__ import annotations

from decimal import Decimal

# (display name, code, rate-or-None). Order preserved for the UI dropdown.
TAX_CATEGORIES: tuple[tuple[str, str, str | None], ...] = (
    ("Zero Goods and Services Tax", "ZERO_GST", "0"),
    ("Alcohol Excise Tax", "ALCOHOL_EXCISE_TAX", None),
    ("Fuel Excise Tax", "FUEL_EXCISE_TAX", None),
    ("Personal Income Tax", "PERSONAL_INCOME_TAX", None),
    ("Plastic Tax", "PLASTIC_TAX", None),
    ("Import Duty", "IMPORT_DUTY", None),
    ("Standard Goods and Services Tax", "STANDARD_GST", None),
    ("Tobacco Excise Tax", "TOBACCO_EXCISE_TAX", None),
    ("Carbon Tax", "CARBON_TAX", None),
    ("Service Tax", "SERVICE_TAX", None),
    ("Tourism Tax", "TOURISM_TAX", None),
    ("Reduced Value-Added Tax", "REDUCED_VAT", "0.075"),
    ("State Sales Tax", "STATE_SALES_TAX", None),
    ("Local Sales Tax", "LOCAL_SALES_TAX", None),
    ("Medicare Tax", "MEDICARE_TAX", None),
    ("Personal Property Tax", "PERSONAL_PROPERTY_TAX", None),
    ("Export Duty", "EXPORT_DUTY", None),
    ("Reduced Goods and Services Tax", "REDUCED_GST", None),
    ("Standard Value-Added Tax", "STANDARD_VAT", "0.075"),
    ("Zero Value-Added Tax", "ZERO_VAT", "0"),
    ("Corporate Income Tax", "CORPORATE_INCOME_TAX", None),
    ("Social Security Tax", "SOCIAL_SECURITY_TAX", None),
    ("Real Estate Tax", "REAL_ESTATE_TAX", None),
    ("Luxury Tax", "LUXURY_TAX", None),
    ("Withholding Tax", "WITHHOLDING_TAX", None),
    ("Tax Exemption", "EXEMPTED", "0"),
    ("Stamp Duty", "STAMP_DUTY", None),
)

# All valid category codes, in source order (for the selection dropdown).
TAX_CATEGORY_CODES: tuple[str, ...] = tuple(code for _, code, _ in TAX_CATEGORIES)

# Code -> VAT rate, only for categories that carry one (incl. zero-rated/exempt).
TAX_CATEGORY_RATES: dict[str, Decimal] = {
    code: Decimal(rate) for _, code, rate in TAX_CATEGORIES if rate is not None
}

# Backward-compatible aliases for category names used before this reference
# (and in seed templates) so existing data keeps resolving to a rate.
TAX_CATEGORY_ALIASES: dict[str, Decimal] = {
    "EXEMPT": Decimal("0"),
    "ZERO_RATED": Decimal("0"),
}
