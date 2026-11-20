"""Proof-of-delivery acceptance: fulfilment is decided at the recipient's end,
never by the carrier handoff alone."""
import json
from labeldispatch import operations,report,disputes,delivery
from labeldispatch.store import connect


def write(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj),encoding='utf-8');return p


def _parcel(db,parcel='P1'):
    return next(x for x in report.fulfilment(connect(db)) if x['parcel']==parcel)


def _base(tmp_path,service='safe_place',parcel='P1',copy='C1',courier='K',
          address='12 Demo St',confirmation=True,extra_auth=None):
    """Print one label, hand it to a courier, settle the batch, and register
    the service chosen at dispatch."""
    db=tmp_path/'ledger.db'
    operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':copy,'parcel':parcel,'paper_no':'N1','template':'T','required_template':'T','printed':True}]}))
    handoff={'id':'H1','parcel':parcel,'copy':copy,'courier':courier,'seal':'S1',
             'at':'2026-01-01T09:00:00','actual_weight':1,'declared_weight':1,'tolerance':0.1,
             'ack_courier':courier,'ack_copy':copy}
    operations.third(db,write(tmp_path,'handoff.json',{'version':'v1','rows':[handoff]}))
    operations.settle(db,write(tmp_path,'batch.json',{'version':'v1','rows':[{'id':'B1','handoffs':['H1']}]}))
    auth={'id':'A1','parcel':parcel,'service':service,'place':'front door cabinet',
          'address':address,'recipient_confirmed':confirmation,
          'valid_from':'2026-01-01T00:00:00','valid_to':'2026-12-31T00:00:00'}
    if extra_auth:auth.update(extra_auth)
    delivery.confirm(db,write(tmp_path,'auth.json',{'version':'v1','rows':[auth]}))
    return db


def _attempt(db,tmp_path,row):
    return delivery.evaluate(db,write(tmp_path,'attempt.json',{'version':'v1','rows':[row]}))


def test_handoff_alone_is_not_fulfilment(tmp_path):
    db=_base(tmp_path)
    assert connect(db).execute("SELECT status FROM handoff WHERE id='H1'").fetchone()[0]=='已交接'
    p=_parcel(db)
    assert p['status']=='待追投' and p['reason']=='no_physical_attempt'


def test_safe_place_proof_completes_delivery(tmp_path):
    db=_base(tmp_path)
    result=_attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2',
        'door_photo':'sha256:photo-1'})
    assert result[0]['status']=='已履约'
    assert result[0]['reason']=='safe_place_proof_verified'
    assert _parcel(db)['service']=='safe_place'
    # The carrier handoff stays a completed historical fact.
    assert connect(db).execute("SELECT status FROM handoff WHERE id='H1'").fetchone()[0]=='已交接'


def test_face_to_face_photo_without_code_does_not_complete(tmp_path):
    db=_base(tmp_path,service='face_to_face')
    result=_attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2',
        'door_photo':'sha256:photo-1','signer':'recipient'})
    assert result[0]['status']=='待追投'
    assert result[0]['reason']=='code_missing_physical_check'
    assert _parcel(db)['status']=='待追投'
    # Same door material under face-to-face stays "reached the door only".
    assert _parcel(db)['attempts'][0]['status']=='待补'
    assert _parcel(db)['conflicts']==['code_missing_physical_check']


def test_face_to_face_code_and_signature_complete_delivery(tmp_path):
    db=_base(tmp_path,service='face_to_face')
    result=_attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2',
        'door_photo':'sha256:photo-1','signer':'recipient','recipient_code':'OTP-9'})
    assert result[0]['status']=='已履约'
    assert result[0]['reason']=='face_to_face_code_verified'


def test_photo_or_gps_alone_never_proves_delivery(tmp_path):
    db=_base(tmp_path)
    assert _attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2'})[0]['status']=='待追投'
    assert _attempt(db,tmp_path,{'id':'DA2','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:05:00','location':'12 Demo St','door_photo':'sha256:photo-1'})[0]['status']=='待追投'


def test_photo_location_outside_authorized_address_blocks_completion(tmp_path):
    db=_base(tmp_path)
    result=_attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2',
        'door_photo':'sha256:photo-1','photo_location':'99 Elsewhere Rd'})
    assert result[0]['status']=='待追投'
    assert result[0]['conflicts']==['location_mismatch']


def test_old_reprint_photo_cannot_prove_reprinted_parcel(tmp_path):
    db=tmp_path/'ledger.db'
    operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True}]}))
    operations.second(db,write(tmp_path,'reprint.json',{'version':'v1','rows':[{'id':'R1','old_copy':'C1',
        'reason':'damaged','approver':'A','at':'2026-01-01T10:00:00','expires':'2026-01-02T00:00:00',
        'new_copy':'C2','new_paper':'N2'}]}))
    operations.third(db,write(tmp_path,'handoff.json',{'version':'v1','rows':[{'id':'H1','parcel':'P1',
        'copy':'C2','courier':'K','seal':'S1','at':'2026-01-01T09:00:00','actual_weight':1,
        'declared_weight':1,'tolerance':0.1,'ack_courier':'K','ack_copy':'C2'}]}))
    delivery.confirm(db,write(tmp_path,'auth.json',{'version':'v1','rows':[{'id':'A1','parcel':'P1',
        'service':'safe_place','place':'cabinet','address':'12 Demo St','recipient_confirmed':True,
        'valid_from':'2026-01-01T00:00:00','valid_to':'2026-12-31T00:00:00'}]}))
    stale=_attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p'})
    assert stale[0]['status']=='待追投'
    assert 'stale_label' in stale[0]['conflicts']
    # The current label works.
    assert _attempt(db,tmp_path,{'id':'DA2','parcel':'P1','copy':'C2','courier':'K','seal':'S1',
        'at':'2026-01-03T11:00:00','location':'12 Demo St','gps':'geo:1,2',
        'door_photo':'sha256:p2'})[0]['status']=='已履约'


def test_proof_taken_before_the_reprint_cannot_close_the_new_label(tmp_path):
    db=tmp_path/'ledger.db'
    operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True}]}))
    operations.third(db,write(tmp_path,'handoff.json',{'version':'v1','rows':[{'id':'H1','parcel':'P1',
        'copy':'C1','courier':'K','seal':'S1','at':'2026-01-01T09:00:00','actual_weight':1,
        'declared_weight':1,'tolerance':0.1,'ack_courier':'K','ack_copy':'C1'}]}))
    delivery.confirm(db,write(tmp_path,'auth.json',{'version':'v1','rows':[{'id':'A1','parcel':'P1',
        'service':'safe_place','place':'cabinet','address':'12 Demo St','recipient_confirmed':True,
        'valid_from':'2026-01-01T00:00:00','valid_to':'2026-12-31T00:00:00'}]}))
    operations.second(db,write(tmp_path,'reprint.json',{'version':'v1','rows':[{'id':'R1','old_copy':'C1',
        'reason':'damaged','approver':'A','at':'2026-01-05T10:00:00','expires':'2026-01-06T00:00:00',
        'new_copy':'C2','new_paper':'N2'}]}))
    # The superseded label can no longer be handed off again.
    assert operations.third(db,write(tmp_path,'old-handoff.json',{'version':'v1','rows':[
        {'id':'H1b','parcel':'P1','copy':'C1','courier':'K','seal':'S2','at':'2026-01-05T11:00:00',
         'actual_weight':1,'declared_weight':1,'tolerance':0.1,'ack_courier':'K','ack_copy':'C1'}]}))[0][
        'reason']=='parcel_already_handed_off'
    # The reprinted identity is dispatched in a new cycle.
    new_handoff=operations.third(db,write(tmp_path,'new-handoff.json',{'version':'v1','rows':[
        {'id':'H2','parcel':'P1','copy':'C2','courier':'K','seal':'S2','at':'2026-01-06T09:00:00',
         'actual_weight':1,'declared_weight':1,'tolerance':0.1,'ack_courier':'K','ack_copy':'C2'}]}))
    assert new_handoff[0]['reason']=='carrier_verified_after_reprint'
    before=_attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C2','courier':'K','seal':'S1',
        'at':'2026-01-04T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:old'})
    assert before[0]['status']=='待追投'
    assert before[0]['conflicts']==['attempt_before_current_cycle']
    # A door photo from before the reprint still cannot close the new cycle.
    stale=_attempt(db,tmp_path,{'id':'DA2','parcel':'P1','copy':'C2','courier':'K','seal':'S2',
        'at':'2026-01-05T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:old'})
    assert stale[0]['status']=='待追投'
    after=_attempt(db,tmp_path,{'id':'DA3','parcel':'P1','copy':'C2','courier':'K','seal':'S2',
        'at':'2026-01-07T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:new'})
    assert after[0]['status']=='已履约'


def test_location_mismatch_without_gps_or_photo_keeps_physical_reason(tmp_path):
    db=_base(tmp_path)
    result=_attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'99 Elsewhere Rd','gps':'geo:9,9',
        'door_photo':'sha256:p'})
    assert result[0]['status']=='待追投' and result[0]['reason']=='location_mismatch'


def test_settlement_item_carries_end_fulfilment_without_rewriting_handoff(tmp_path):
    db=_base(tmp_path)
    _attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p'})
    operations.settle(db,write(tmp_path,'batch2.json',{'version':'v1','rows':[{'id':'B2','handoffs':['H1']}]}))
    item=json.loads(connect(db).execute("SELECT body FROM settlement_item WHERE id='B2:H1'").fetchone()[0])
    assert item['fulfilment']=='已履约' and item['fulfilment_reason']=='safe_place_proof_verified'
    assert connect(db).execute("SELECT status FROM handoff WHERE id='H1'").fetchone()[0]=='已交接'


def test_attempt_declaring_a_different_service_is_not_accepted(tmp_path):
    db=_base(tmp_path,service='face_to_face')
    result=_attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p',
        'service':'safe_place'})
    assert result[0]['status']=='待追投' and result[0]['reason']=='service_mismatch'


def test_second_handoff_of_same_parcel_and_label_is_refused(tmp_path):
    db=_base(tmp_path)
    duplicate=operations.third(db,write(tmp_path,'again.json',{'version':'v1','rows':[
        {'id':'H2','parcel':'P1','copy':'C1','courier':'J','seal':'S2','at':'2026-01-02T09:00:00',
         'actual_weight':1,'declared_weight':1,'tolerance':0.1,'ack_courier':'J','ack_copy':'C1'}]}))
    assert duplicate[0]['status']=='拒交' and duplicate[0]['reason']=='parcel_already_handed_off'
    assert _parcel(db)['handoff']=='H1'


def test_code_replayed_on_two_parcels_only_credits_the_first(tmp_path):
    db=tmp_path/'ledger.db'
    operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True},
        {'id':'C2','parcel':'P2','paper_no':'N2','template':'T','required_template':'T','printed':True}]}))
    operations.third(db,write(tmp_path,'handoff.json',{'version':'v1','rows':[
        {'id':'H1','parcel':'P1','copy':'C1','courier':'K','seal':'S1','at':'2026-01-01T09:00:00',
         'actual_weight':1,'declared_weight':1,'tolerance':0.1,'ack_courier':'K','ack_copy':'C1'},
        {'id':'H2','parcel':'P2','copy':'C2','courier':'K','seal':'S2','at':'2026-01-01T09:00:00',
         'actual_weight':1,'declared_weight':1,'tolerance':0.1,'ack_courier':'K','ack_copy':'C2'}]}))
    auth={'version':'v1','rows':[
        {'id':'A1','parcel':'P1','service':'face_to_face','address':'1 A St','recipient_confirmed':True,
         'valid_from':'2026-01-01T00:00:00','valid_to':'2026-12-31T00:00:00'},
        {'id':'A2','parcel':'P2','service':'face_to_face','address':'2 B St','recipient_confirmed':True,
         'valid_from':'2026-01-01T00:00:00','valid_to':'2026-12-31T00:00:00'}]}
    delivery.confirm(db,write(tmp_path,'auth.json',auth))
    result=delivery.evaluate(db,write(tmp_path,'attempts.json',{'version':'v1','rows':[
        {'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1','at':'2026-01-02T10:00:00',
         'location':'1 A St','signer':'R1','recipient_code':'OTP-1'},
        {'id':'DA2','parcel':'P2','copy':'C2','courier':'K','seal':'S2','at':'2026-01-02T10:30:00',
         'location':'2 B St','signer':'R2','recipient_code':'OTP-1'}]}))
    by={x['parcel']:x for x in result}
    assert by['P1']['status']=='已履约'
    assert by['P2']['status']=='待追投' and by['P2']['reason']=='code_replayed'


def test_refusal_then_successful_redelivery_keeps_both_outcomes(tmp_path):
    db=_base(tmp_path)
    delivery.evaluate(db,write(tmp_path,'a1.json',{'version':'v1','rows':[
        {'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1','at':'2026-01-02T10:00:00',
         'outcome':'refused','location':'12 Demo St'}]}))
    assert _parcel(db)['status']=='待追投'
    assert _parcel(db)['reason']=='recipient_refused'
    delivery.evaluate(db,write(tmp_path,'a2.json',{'version':'v1','rows':[
        {'id':'DA2','parcel':'P1','copy':'C1','courier':'K','seal':'S1','at':'2026-01-03T10:00:00',
         'location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p2'}]}))
    p=_parcel(db)
    assert p['status']=='已履约'
    conn=connect(db)
    assert conn.execute("SELECT status FROM delivery_outcome WHERE id='DA1'").fetchone()[0]=='不计成功'
    assert conn.execute("SELECT reason FROM delivery_outcome WHERE id='DA1'").fetchone()[0]=='recipient_refused'
    assert conn.execute("SELECT status FROM delivery_outcome WHERE id='DA2'").fetchone()[0]=='计为成功'


def test_late_receipt_is_not_chosen_by_upload_order(tmp_path):
    db=_base(tmp_path)
    # A successful earlier attempt is uploaded last; event time must win.
    delivery.evaluate(db,write(tmp_path,'late-success.json',{'version':'v1','rows':[
        {'id':'DA2','parcel':'P1','copy':'C1','courier':'K','seal':'S1','at':'2026-01-04T10:00:00',
         'location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p2'}]}))
    delivery.evaluate(db,write(tmp_path,'old-refusal.json',{'version':'v1','rows':[
        {'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1','at':'2026-01-02T10:00:00',
         'outcome':'refused','location':'12 Demo St'}]}))
    p=_parcel(db)
    assert p['status']=='已履约'
    assert [x['attempt'] for x in p['attempts']]==['DA1','DA2']
    assert p['attempts'][0]['status']=='不计成功'
    assert p['attempts'][1]['status']=='计为成功'


def test_duplicate_import_does_not_rewrite_physical_attempts(tmp_path):
    db=_base(tmp_path)
    body={'version':'v1','rows':[{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p'}]}
    first=_attempt(db,tmp_path,body)
    second=_attempt(db,tmp_path,body)
    assert first==second
    conn=connect(db)
    assert conn.execute('SELECT count(*) FROM delivery_attempt').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM export_file WHERE kind=\'delivery_evidence\'').fetchone()[0]==1


def test_new_handoff_starts_a_new_cycle_and_old_proof_stops_counting(tmp_path):
    db=_base(tmp_path)
    _attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p'})
    assert _parcel(db)['status']=='已履约'
    # A dispute recovers the parcel; a second, later handoff is accepted.
    disputes.claim(db,write(tmp_path,'claim.json',{'version':'v1','rows':[
        {'id':'D1','handoff':'H1','observed_copy':'foreign','operator':'K'}]}))
    assert _parcel(db)['status']=='待追投'
    assert _parcel(db)['reason']=='handoff_under_dispute'
    assert _parcel(db)['handoff_count']==1
    # The old success can no longer close the parcel while the dispute stands.
    assert connect(db).execute("SELECT status FROM delivery_outcome WHERE id='DA1'").fetchone()[0]=='不计成功'


def test_no_service_authorization_means_no_fulfilment(tmp_path):
    db=tmp_path/'ledger.db'
    operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True}]}))
    operations.third(db,write(tmp_path,'handoff.json',{'version':'v1','rows':[
        {'id':'H1','parcel':'P1','copy':'C1','courier':'K','seal':'S1','at':'2026-01-01T09:00:00',
         'actual_weight':1,'declared_weight':1,'tolerance':0.1,'ack_courier':'K','ack_copy':'C1'}]}))
    result=_attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p'})
    assert result[0]['status']=='待追投'
    assert result[0]['conflicts']==['authorization_missing']


def test_report_answers_fulfilled_and_awaiting_redelivery_with_sources(tmp_path):
    db=_base(tmp_path)
    _attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p'})
    data=json.loads(report.build(db,tmp_path/'report.json').read_text(encoding='utf-8'))
    assert data['fulfilled']==['P1'] and data['awaiting_redelivery']==[]
    p=data['fulfilment'][0]
    assert p['service']=='safe_place' and p['authorization']=='A1'
    assert p['authorization_scope']['place']=='front door cabinet'
    assert p['attempts'][0]['reason']=='safe_place_proof_verified'
    kinds={s['kind'] for s in p['sources']}
    assert {'delivery_authorization','delivery_evidence','carrier_handoff'}<=kinds
    assert all(s['verified'] for s in p['sources'])
    latest=data['entities']['delivery_assessment'][-1]
    assert latest['status']=='已履约' and latest['id'].startswith('P1:H1:')


def test_redelivery_conclusion_reaches_the_next_settlement_cycle(tmp_path):
    db=_base(tmp_path)
    _attempt(db,tmp_path,{'id':'DA1','parcel':'P1','copy':'C1','courier':'K','seal':'S1',
        'at':'2026-01-02T11:00:00','location':'12 Demo St','gps':'geo:1,2','door_photo':'sha256:p'})
    # A later settlement batch reads current fulfilment without rewriting history.
    operations.settle(db,write(tmp_path,'batch2.json',{'version':'v1','rows':[{'id':'B2','handoffs':['H1']}]}))
    conn=connect(db)
    assert conn.execute("SELECT status FROM settlement_item WHERE id='B2:H1'").fetchone()[0]=='已结算'
    assert conn.execute("SELECT status FROM delivery_cycle WHERE id='P1:H1'").fetchone()[0]=='已履约'
    decisions=[dict(r) for r in conn.execute("SELECT * FROM decision_log WHERE subsystem='末端投递'")]
    assert any(x['subject']=='P1' and x['status']=='已履约' for x in decisions)


def test_cli_exposes_delivery_commands():
    from labeldispatch import __main__ as cli
    assert 'service-confirm' in cli.COMMANDS and 'delivery-import' in cli.COMMANDS
    assert len(cli.COMMANDS)==len(cli.FUNCTIONS)
