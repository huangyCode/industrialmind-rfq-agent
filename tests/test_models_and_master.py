from rfq_agent.data.repositories import CatalogRepo, MaterialRepo, RateRepo
from rfq_agent.models import DrawingSpec, RFQRequest


def test_schemas_export():
    assert "title_block" in DrawingSpec.model_json_schema()["properties"]
    assert "items" in RFQRequest.model_json_schema()["properties"]


def test_master_counts(db):
    assert db.execute("SELECT COUNT(*) FROM materials").fetchone()[0] == 6
    assert db.execute("SELECT COUNT(*) FROM plants").fetchone()[0] == 3


def test_material_alias(db):
    m = MaterialRepo(db)
    assert m.resolve("1.7225") == "42CrMo4"
    assert m.resolve("42CrMo4+QT") == "42CrMo4"
    assert m.resolve("EN AW-6082 T6") == "AW6082"
    assert m.resolve("16MnCr5") == "16MnCr5"
    assert m.resolve("Unobtainium") is None
    assert m.resolve(None) is None


def test_rates_and_capability(db):
    r = RateRepo(db)
    assert r.rate("DE", "CNC_TURN") == 95
    assert r.rate("CN", "GRIND_CYL") is None


def test_catalog_match(db):
    c = CatalogRepo(db)
    assert c.match("6204-2RS", "Rillenkugellager", "DIN 625")[0]["part_number"] == "6204-2RS"
    row, how = c.match(None, "Zylinderschraube M5 x 16", "ISO 4762")
    assert row["part_number"] == "ISO4762-M5x16" and how == "normalized"
    assert c.match(None, "Wellendichtring 20x35x7", "DIN 3760")[0]["part_number"] == "SEAL-20x35x7"
    assert c.match("EC-5103", "End cover", None) is None
