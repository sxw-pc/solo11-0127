import json
from labeldispatch import operations,report,resources,disputes
from labeldispatch.store import connect

def write(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj),encoding='utf-8');return p

def test_print_reprint_handoff_full_chain(tmp_path):
    result=operations.demo(tmp_path)
    assert [x[0]['status'] for x in result]==['有效','批准','已交接','已结算']
    db=tmp_path/'ledger.db';conn=connect(db)
    assert conn.execute("SELECT status FROM print_copy WHERE id='C1'").fetchone()[0]=='作废'
    assert conn.execute("SELECT status FROM print_copy WHERE id='C2'").fetchone()[0]=='有效'
    data=json.loads(report.build(db,tmp_path/'report.json').read_text(encoding='utf-8'))
    assert len(data['sources'])==5 and all(s['verified'] for s in data['sources'])
    assert conn.execute("SELECT count(*) FROM printer_reservation WHERE status='已消耗'").fetchone()[0]==2

def test_print_copy_identity_template_and_missing_receipt(tmp_path):
    db=tmp_path/'ledger.db'
    rows=[{'id':'C1','parcel':'P1','paper_no':'N1','template':'old','required_template':'new','printed':True},
          {'id':'C2','parcel':'P2','paper_no':'N2','template':'T','required_template':'T','printed':False},
          {'id':'C3','parcel':'P3','paper_no':'N1','template':'T','required_template':'T','printed':True}]
    result=operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['template_mismatch','receipt_missing','paper_reused']
    assert connect(db).execute('SELECT count(*) FROM print_copy').fetchone()[0]==3
    assert connect(db).execute("SELECT status FROM print_copy WHERE id='C1'").fetchone()[0]=='冲突'

def test_unauthorized_reprint_does_not_void_original(tmp_path):
    db=tmp_path/'ledger.db';operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True}]}))
    bad={'version':'v1','rows':[{'id':'R1','old_copy':'C1','reason':'damaged','approver':'A',
          'at':'2026-01-03T00:00:00','expires':'2026-01-02T00:00:00','new_copy':'C2','new_paper':'N2'},
          {'id':'R2','old_copy':'C1','reason':'convenience','approver':'A','at':'2026-01-01T10:00:00',
           'expires':'2026-01-02T00:00:00','new_copy':'C3','new_paper':'N3'}]}
    assert [x['reason'] for x in operations.second(db,write(tmp_path,'bad.json',bad))]==['authorization_invalid','unsupported_reason']
    assert connect(db).execute("SELECT status FROM print_copy WHERE id='C1'").fetchone()[0]=='有效'

def test_weight_and_carrier_ack_change_handoff(tmp_path):
    db=tmp_path/'ledger.db';operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True},
        {'id':'C2','parcel':'P2','paper_no':'N2','template':'T','required_template':'T','printed':True}]}))
    rows=[{'id':'H1','parcel':'P1','copy':'C1','actual_weight':3,'declared_weight':1,'tolerance':0.1,'courier':'K','ack_courier':'K','ack_copy':'C1'},
          {'id':'H2','parcel':'P2','copy':'C2','actual_weight':1,'declared_weight':1,'tolerance':0.1,'courier':'K','ack_courier':'other','ack_copy':'C2'}]
    assert [x['status'] for x in operations.third(db,write(tmp_path,'handoff.json',{'version':'v1','rows':rows}))]==['拒交','待核']

def test_source_replay_is_content_addressed(tmp_path):
    db=tmp_path/'ledger.db';file=write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True}]})
    operations.first(db,file);operations.first(db,file)
    conn=connect(db)
    assert conn.execute('SELECT count(*) FROM export_file').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM print_copy').fetchone()[0]==1

def test_reprint_replay_keeps_authorized_copy(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True}]}))
    file=write(tmp_path,'reprint.json',{'version':'v1','rows':[{'id':'R1','old_copy':'C1','reason':'damaged','approver':'A',
        'at':'2026-01-01T10:00:00','expires':'2026-01-02T00:00:00','new_copy':'C2','new_paper':'N2'}]})
    assert operations.second(db,file)==operations.second(db,file)
    assert connect(db).execute('SELECT count(*) FROM print_copy').fetchone()[0]==2

def test_batch_settlement_preserves_partial_failure(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True}]}))
    operations.third(db,write(tmp_path,'handoff.json',{'version':'v1','rows':[
        {'id':'H1','parcel':'P1','copy':'C1','actual_weight':3,'declared_weight':1,'tolerance':0.1,'courier':'K','ack_courier':'K','ack_copy':'C1'}]}))
    result=operations.settle(db,write(tmp_path,'batch.json',{'version':'v1','rows':[{'id':'B1','handoffs':['H1']}]}))
    assert result[0]['status']=='部分失败'
    assert connect(db).execute("SELECT status FROM handoff_batch WHERE id='B1'").fetchone()[0]=='部分失败'

def test_printer_capacity_template_and_paper_stock_are_independent(tmp_path):
    db=tmp_path/'db';plan={'version':'v1','printers':[{'id':'P1','templates':['T1'],'paper':{'A6':1},'capacity':1,'online':True}],
        'jobs':[{'copy_id':'C1','template':'T1','paper':'A6'},
                {'copy_id':'C2','template':'T1','paper':'A6'},
                {'copy_id':'C3','template':'T2','paper':'A6'}]}
    assert [x['status'] for x in resources.plan(db,write(tmp_path,'plan.json',plan))]==['已预留','待资源','待资源']
    spool={'version':'v1','rows':[{'id':'C1','parcel':'P1','paper_no':'N1','template':'T1','required_template':'T1','printed':True},
                                  {'id':'C2','parcel':'P2','paper_no':'N2','template':'T1','required_template':'T1','printed':True}]}
    assert [x['reason'] for x in operations.first(db,write(tmp_path,'spool.json',spool))]==['printed_and_unique','printer_reservation_missing']
    assert connect(db).execute("SELECT status FROM printer_reservation WHERE id='C1'").fetchone()[0]=='已消耗'

def test_operator_cancel_requires_physical_evidence(tmp_path):
    db=tmp_path/'db';plan={'version':'v1','printers':[{'id':'P1','templates':['T'],'paper':{'A6':2},'capacity':2,'online':True}],
        'jobs':[{'copy_id':'C1','template':'T','paper':'A6'}]}
    resources.plan(db,write(tmp_path,'plan.json',plan))
    assert resources.cancel(db,write(tmp_path,'bad.json',{'version':'v1','rows':[{'copy_id':'C1','operator':'A','evidence':'unknown'}]}))[0]['status']=='待核'
    assert resources.cancel(db,write(tmp_path,'ok.json',{'version':'v1','rows':[{'copy_id':'C1','operator':'A','evidence':'no_paper_output'}]}))[0]['status']=='已释放'
    spool={'version':'v1','rows':[{'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':True}]}
    assert operations.first(db,write(tmp_path,'spool.json',spool))[0]['status']=='待资源'

def test_carrier_dispute_reopens_settled_batch_and_tracks_recall(tmp_path):
    db=tmp_path/'ledger.db';operations.demo(tmp_path)
    same={'version':'v1','rows':[{'id':'D0','handoff':'H1','observed_copy':'C2','operator':'K'}]}
    assert disputes.claim(db,write(tmp_path,'same.json',same))[0]['status']=='驳回'
    mismatch={'version':'v1','rows':[{'id':'D1','handoff':'H1','observed_copy':'foreign','operator':'K'}]}
    file=write(tmp_path,'dispute.json',mismatch)
    assert disputes.claim(db,file)==disputes.claim(db,file)
    conn=connect(db)
    assert conn.execute("SELECT status FROM handoff_batch WHERE id='B1'").fetchone()[0]=='争议重开'
    assert conn.execute("SELECT status FROM parcel WHERE id='P1'").fetchone()[0]=='召回中'
    missing={'version':'v1','rows':[{'claim':'D1','parcel_returned':False,'operator':'K','evidence':''}]}
    assert disputes.resolve(db,write(tmp_path,'missing.json',missing))[0]['status']=='待核'
    returned={'version':'v1','rows':[{'claim':'D1','parcel_returned':True,'operator':'K','evidence':'scan:P1'}]}
    assert disputes.resolve(db,write(tmp_path,'returned.json',returned))[0]['status']=='已召回'
    assert connect(db).execute("SELECT status FROM recall_case WHERE id='recall:D1'").fetchone()[0]=='已召回'
    assert operations.settle(db,tmp_path/'settlement.json')[0]['status']=='争议重开'
    retry={'version':'v1','rows':[{'id':'H2','parcel':'P1','copy':'C2','actual_weight':1,
        'declared_weight':1,'tolerance':0.1,'courier':'K','ack_courier':'K','ack_copy':'C2'}]}
    assert operations.third(db,write(tmp_path,'retry.json',retry))[0]['reason']=='recall_open'

def test_reprint_requires_active_original_and_unused_replacement_identity(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'spool.json',{'version':'v1','rows':[
        {'id':'C1','parcel':'P1','paper_no':'N1','template':'T','required_template':'T','printed':False},
        {'id':'C2','parcel':'P2','paper_no':'N2','template':'T','required_template':'T','printed':True}]}))
    rows=[{'id':'R1','old_copy':'C1','reason':'damaged','approver':'A','at':'2026-01-01T10:00:00',
           'expires':'2026-01-02T00:00:00','new_copy':'C3','new_paper':'N3'},
          {'id':'R2','old_copy':'C2','reason':'damaged','approver':'A','at':'2026-01-01T10:00:00',
           'expires':'2026-01-02T00:00:00','new_copy':'C1','new_paper':'N4'},
          {'id':'R3','old_copy':'C2','reason':'damaged','approver':'A','at':'2026-01-01T10:00:00',
           'expires':'2026-01-02T00:00:00','new_copy':'C4','new_paper':'N2'}]
    result=operations.second(db,write(tmp_path,'requests.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['old_copy_not_active','replacement_identity_conflict','paper_reused']
    assert connect(db).execute("SELECT status FROM print_copy WHERE id='C2'").fetchone()[0]=='有效'
