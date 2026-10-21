import json

import inbound

SECRET = "test-secret"
FIRMS = {"contoso.example": {"company": "Contoso", "employees": 2400, "industry": "Software", "country": "US"},
         "northwind.example": {"company": "Northwind", "employees": 120, "industry": "Retail", "country": "US"}}


def post(conn, payload, secret=SECRET, signature=None):
    body = json.dumps(payload).encode()
    sig = signature if signature is not None else inbound.sign(body, secret)
    return inbound.handle(body, sig, SECRET, conn, FIRMS)


def lead(**kw):
    return {"email": "jamie@contoso.example", "form_id": "demo_request", "title": "VP Sales", **kw}


def test_rejects_missing_and_wrong_signatures():
    conn = inbound.open_db(":memory:")
    assert post(conn, lead(), signature="")[0] == 401
    assert post(conn, lead(), secret="wrong")[0] == 401


def test_empty_server_secret_rejects_everything():
    body = json.dumps(lead()).encode()
    assert inbound.handle(body, inbound.sign(body, ""), "", inbound.open_db(":memory:"), FIRMS)[0] == 401


def test_rejects_invalid_payloads():
    conn = inbound.open_db(":memory:")
    status, resp = post(conn, {"email": "not-an-email"})
    assert status == 400 and len(resp["problems"]) == 2
    body = b"not json"
    assert inbound.handle(body, inbound.sign(body, SECRET), SECRET, conn, FIRMS)[0] == 400


def test_hot_enterprise_lead_is_enriched_scored_and_routed(capsys):
    conn = inbound.open_db(":memory:")
    status, resp = post(conn, lead())
    assert status == 202
    assert (resp["company"], resp["score"], resp["owner"]) == ("Contoso", 100, "ae_enterprise")
    assert "[slack dry-run] :fire: Hot lead jamie@contoso.example" in capsys.readouterr().err


def test_duplicate_submission_is_a_no_op():
    conn = inbound.open_db(":memory:")
    assert post(conn, lead())[0] == 202
    status, resp = post(conn, lead(email="JAMIE@contoso.example"))
    assert (status, resp["status"]) == (200, "duplicate")
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1


def test_free_email_goes_to_nurture_without_alert(capsys):
    conn = inbound.open_db(":memory:")
    status, resp = post(conn, lead(email="someone@gmail.com"))
    assert (status, resp["score"], resp["owner"]) == (202, 0, "nurture")
    assert capsys.readouterr().err == ""


def test_unknown_company_mid_score_goes_to_sdr_queue():
    conn = inbound.open_db(":memory:")
    _, resp = post(conn, lead(email="a@unknown.example", form_id="pricing"))
    assert (resp["score"], resp["owner"]) == (40, "sdr_queue")
