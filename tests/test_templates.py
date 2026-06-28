import csv
import io

from core.masters import ItemEntry, PartyEntry
from core.output import (
    ITEM_TEMPLATE_COLUMNS,
    PARTY_TEMPLATE_COLUMNS,
    write_items_template,
    write_parties_template,
)


def test_item_template_header_exact_order():
    text = write_items_template([
        ItemEntry(name="Widget", item_code="ITM_207", hsn_code="2202.99",
                  tax_category="STANDARD_VAT", item_category="Food and Beverages",
                  description="A widget", is_service=False),
    ])
    rows = list(csv.reader(io.StringIO(text)))
    assert tuple(rows[0]) == ITEM_TEMPLATE_COLUMNS
    rec = list(csv.DictReader(io.StringIO(text)))[0]
    assert rec["item_name"] == "Widget"
    assert rec["item_code"] == "ITM_207"
    assert rec["is_service"] == "FALSE"


def test_party_template_header_exact_order_matches_digitax():
    assert PARTY_TEMPLATE_COLUMNS == (
        "tax_identification_number", "email_address", "name", "phone_number(optional)",
        "street_name", "city_name", "postal_zone", "country", "local_government", "state",
    )
    text = write_parties_template([
        PartyEntry(name="John Doe", tin="01234567-0001", status="B2B",
                   email_address="john@doe.com", street_name="32, owonikoko street",
                   city_name="Gwarikpa", postal_zone="23401", country="NGA",
                   local_government="NG-LA-IKO", state="NG-LA"),
    ])
    rows = list(csv.reader(io.StringIO(text)))
    assert tuple(rows[0]) == PARTY_TEMPLATE_COLUMNS
    rec = list(csv.DictReader(io.StringIO(text)))[0]
    assert rec["tax_identification_number"] == "01234567-0001"
    assert rec["country"] == "NGA"
    assert rec["state"] == "NG-LA"
