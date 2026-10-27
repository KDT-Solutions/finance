from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Document, Rule, Supplier, Tenant
from app.services import rules


def _db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_variant_lookup_and_merge():
    db = _db()
    t = Tenant(code="T", name="T", zammad_group_paid="P", zammad_group_topay="Q")
    db.add(t)
    db.flush()
    a = Supplier(tenant_id=t.id, display_name="Mobile World Communications GmbH", name_key=rules.normalize_name("Mobile World Communications GmbH"),
                 ibans=["DE61100800000221061700"], name_variants=["MWC - Mobile World Communications GmbH"], bexio_contact_id=405)
    b = Supplier(tenant_id=t.id, display_name="MWC – Mobile World Communications GmbH", name_key="x", ibans=[], name_variants=[],
                 bexio_contact_id=550, street="Kollwitzstraße 76", postcode="10435", city="Berlin", country="DE")
    db.add_all([a, b])
    db.flush()
    assert rules.find_supplier_by_name(db, t.id, "MWC – Mobile World Communications GmbH") is a
    db.add(Rule(tenant_id=t.id, supplier_id=b.id, keywords=[]))
    db.flush()
    out = rules.merge_suppliers(db, b, a)
    db.refresh(a)
    assert out["rules"] == 1 and a.city == "Berlin" and a.bexio_contact_id == 405
    assert out["source_bexio_contact_id"] == 550
    assert rules.find_supplier_by_name(db, t.id, "x") is None


def test_dedup_plan():
    from app.services.contact_dedup import plan

    g = {"key": "x", "contacts": [
        {"id": 1, "bills_open": [], "bills_paid": 3, "expenses": 0, "sales_documents": 0},
        {"id": 2, "bills_open": [{"id": "a"}], "bills_paid": 0, "expenses": 0, "sales_documents": 0},
        {"id": 3, "bills_open": [], "bills_paid": 0, "expenses": 0, "sales_documents": 0},
        {"id": 4, "bills_open": [], "bills_paid": 1, "expenses": 0, "sales_documents": 0}]}
    plan(g, 1)
    assert [r["action"] for r in g["contacts"]] == ["main", "move", "archive", "mark"]


def test_dedup_plan_archive_paid():
    from app.services.contact_dedup import plan

    g = {"key": "x", "contacts": [
        {"id": 1, "bills_open": [], "bills_paid": 3, "expenses": 0, "sales_documents": 0},
        {"id": 2, "bills_open": [], "bills_paid": 5, "expenses": 1, "sales_documents": 0},
        {"id": 3, "bills_open": [], "bills_paid": 1, "expenses": 0, "sales_documents": None},
        {"id": 4, "bills_open": [], "bills_paid": 1, "expenses": 0, "sales_documents": 2},
        {"id": 5, "bills_open": [], "bills_paid": 0, "expenses": 0, "sales_documents": 0, "archived": True}]}
    plan(g, 1, archive_paid=True)
    assert [r["action"] for r in g["contacts"]] == ["main", "archive", "mark", "mark", "none"]
    plan(g, 1, sales_checked={3}, archive_paid=True)
    assert g["contacts"][2]["action"] == "archive"


def test_merged_supplier_is_not_matched_by_uid():
    # sipcall: the provision created "Backbone Solutions AG (sipcall.ch)" with the UID, merged into the existing supplier
    db = _db()
    t = Tenant(code="T", name="T", zammad_group_paid="P", zammad_group_topay="Q")
    db.add(t)
    db.flush()
    target = Supplier(tenant_id=t.id, display_name="sipcall by Backbone Solutions AG", name_key=rules.normalize_name("sipcall by Backbone Solutions AG"),
                      ibans=[], name_variants=[], bexio_contact_id=20)
    source = Supplier(tenant_id=t.id, display_name="Backbone Solutions AG (sipcall.ch)", name_key=rules.normalize_name("Backbone Solutions AG (sipcall.ch)"),
                      ibans=[], name_variants=[], uid="CHE-102.755.328")
    db.add_all([target, source])
    db.flush()
    rules.merge_suppliers(db, source, target)
    assert target.uid == "CHE-102.755.328" and source.uid is None
    assert rules.match_supplier(db, t.id, uid="CHE-102.755.328", name="Backbone Solutions AG (sipcall.ch)") == (target, "uid")
    # merges made before this fix kept the source UID: the merged record is skipped anyway
    source.uid = "CHE-102.755.328"
    db.flush()
    assert rules.match_supplier(db, t.id, uid="CHE-102.755.328")[0] is target
