def auth(client,username):
    r=client.post("/auth/login",json={"username":username,"password":"ChangeMe-2026!"})
    assert r.status_code==200,r.text
    return {"Authorization":f"Bearer {r.json()['access_token']}"}


def test_login_rbac_balance_discrepancy_and_audit(client):
    inc=auth(client,"inc_op1")
    assert client.get("/users",headers=inc).status_code==403
    batch=client.post("/batches",headers=inc,json={"batch_number":"LOT-001","stage":"INCUBATION","initial_quantity":200}).json()
    draft=client.post("/transfers",headers=inc,json={"batch_id":batch["id"],"sent_quantity":201})
    assert draft.status_code==409
    transfer=client.post("/transfers",headers=inc,json={"batch_id":batch["id"],"sent_quantity":200}).json()
    assert client.post(f"/transfers/{transfer['id']}/send",headers=inc).status_code==200
    grow=auth(client,"grow_op1")
    receive=client.post(f"/transfers/{transfer['id']}/receive",headers=grow,json={"received_quantity":198,"comment":"Повреждение при транспортировке"})
    assert receive.status_code==200,receive.text
    assert receive.json()["discrepancy"]==2 and receive.json()["status"]=="DISPUTED"
    assert client.get("/batches",headers=grow).json()[0]["available_quantity"]==0
    lead=auth(client,"grow_lead")
    confirmed=client.post(f"/transfers/{transfer['id']}/confirm",headers=lead,json={"reason":"Сверено по акту приемки"})
    assert confirmed.status_code==200,confirmed.text
    assert confirmed.json()["status"]=="CONFIRMED"
    assert client.post(f"/transfers/{transfer['id']}/confirm",headers=lead,json={}).status_code==409
    manager=auth(client,"manager")
    dash=client.get("/dashboard",headers=manager).json()
    growing=next(row for row in dash["stages"] if row["stage"]=="GROWING")
    assert growing["received"]==198
    assert dash["first_discrepancy"]["quantity"]==2
    audit=client.get("/audit-logs",headers=manager)
    assert audit.status_code==200 and len(audit.json())>=5
    result=client.get("/audit-logs/verify",headers=auth(client,"admin")).json()
    assert result["valid"] is True, result


def test_loss_requires_other_comment_and_lead_review(client):
    inc=auth(client,"inc_op1")
    batch=client.post("/batches",headers=inc,json={"batch_number":"LOT-LOSS","stage":"INCUBATION","initial_quantity":100}).json()
    invalid=client.post("/losses",headers=inc,json={"batch_id":batch["id"],"quantity":12,"reason":"OTHER"})
    assert invalid.status_code==422
    loss=client.post("/losses",headers=inc,json={"batch_id":batch["id"],"quantity":12,"reason":"DISEASE"}).json()
    assert loss["status"]=="PENDING"
    lead=auth(client,"grow_lead")
    assert client.post(f"/losses/{loss['id']}/approve",headers=lead).status_code==403


def test_large_loss_approver_must_be_different_user(client):
    lead=auth(client,"grow_lead")
    batch=client.post("/batches",headers=lead,json={"batch_number":"LOT-LEAD","stage":"GROWING","initial_quantity":100}).json()
    loss=client.post("/losses",headers=lead,json={"batch_id":batch["id"],"quantity":12,"reason":"DISEASE"}).json()
    assert client.post(f"/losses/{loss['id']}/approve",headers=lead).status_code==403


def test_login_lockout_and_change_password(client):
    for _ in range(5):
        assert client.post("/auth/login",json={"username":"inc_op1","password":"wrong"}).status_code==401
    assert client.post("/auth/login",json={"username":"inc_op1","password":"ChangeMe-2026!"}).status_code==429

