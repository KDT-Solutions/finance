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
