"""Carrier reception disputes and physical parcel recall after settlement."""
import json
from .ingest import capture
from .store import connect,save,decide


def claim(path,file):
    source,payload=capture(path,'carrier_dispute_export',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);old=db.execute('SELECT * FROM carrier_claim WHERE id=?',(key,)).fetchone()
                if old:
                    if old['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):raise ValueError('claim_identity_conflict')
                    results.append(decide(db,'承运争议',key,old['status'],old['reason'],source));continue
                handoff=db.execute('SELECT * FROM handoff WHERE id=?',(str(row['handoff']),)).fetchone()
                if not handoff:status,reason='待核','handoff_missing'
                elif handoff['status']!='已交接':status,reason='待核','handoff_not_final'
                elif not row.get('operator'):status,reason='待核','operator_missing'
                elif str(row.get('observed_copy'))==str(json.loads(handoff['body'])['copy']):status,reason='驳回','copy_matches'
                else:status,reason='争议成立','copy_discrepancy'
                save(db,'carrier_claim',key,source,payload['version'],row,status,reason)
                if status=='争议成立':
                    h=json.loads(handoff['body']);parcel_id=str(h['parcel'])
                    db.execute("UPDATE handoff SET status='争议',reason='carrier_dispute' WHERE id=?",(handoff['id'],))
                    db.execute("UPDATE parcel SET status='召回中',reason='carrier_dispute' WHERE id=?",(parcel_id,))
                    save(db,'recall_case',f'recall:{key}',source,payload['version'],
                         {'claim':key,'handoff':handoff['id'],'parcel':parcel_id},'追索中','copy_discrepancy')
                    batches=[x['id'] for x in db.execute('SELECT * FROM handoff_batch') if handoff['id'] in json.loads(x['body'])['handoffs']]
                    for batch_id in batches:
                        db.execute("UPDATE handoff_batch SET status='争议重开',reason='carrier_dispute' WHERE id=?",(batch_id,))
                    from . import billing
                    billing.on_handoff_disputed(db,handoff['id'],source,payload['version'])
                results.append(decide(db,'承运争议',key,status,reason,source))
        return results
    finally:db.close()


def resolve(path,file):
    source,payload=capture(path,'carrier_recall_export',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['claim']);case=db.execute('SELECT * FROM recall_case WHERE id=?',(f'recall:{key}',)).fetchone()
                if not case or case['status'] not in ('追索中','已召回'):status,reason='待核','recall_case_missing'
                elif not row.get('evidence') or not row.get('operator'):status,reason='待核','physical_evidence_missing'
                elif row.get('parcel_returned'):
                    status,reason='已召回','parcel_returned'
                    db.execute("UPDATE recall_case SET status='已召回',reason='parcel_returned' WHERE id=?",(f'recall:{key}',))
                else:status,reason='追索中','parcel_outstanding'
                results.append(decide(db,'承运争议',key,status,reason,source))
        return results
    finally:db.close()
