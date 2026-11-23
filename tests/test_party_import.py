"""The parties importer must store Digitax LGA/state CODES derived from the
bundled reference, never the sheet's free-text names."""
import app


def test_geo_codes_derives_from_reference():
    # Free-text state + "... Local Government Area (LGA)" -> NG codes.
    state, lga = app._geo_codes("Lagos", "Mushin Local Government Area (LGA)")
    assert state == "NG-LA"
    assert lga.startswith("NG-LA-") and lga != "NG-LA"

    # Unmatched / junk LGA text -> blank (operator sets it), state still resolves.
    state2, lga2 = app._geo_codes("Abuja", "agiville@gmail.com")
    assert state2 == "NG-FC"
    assert lga2 == ""


def test_geo_codes_keeps_existing_ng_codes():
    assert app._geo_codes("NG-LA", "NG-LA-IKE") == ("NG-LA", "NG-LA-IKE")
