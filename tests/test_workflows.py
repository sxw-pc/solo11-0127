import json
from labeldispatch import operations,report
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
    assert len(data['sources'])==4 and all(s['verified'] for s in data['sources'])

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
