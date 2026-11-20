"""Physical label copies, reprint authority, carrier handoff, and the
per-parcel reconciliation that follows."""
import json
from datetime import datetime
from .ingest import capture
from .store import connect,save,decide
from . import delivery


def _reconcile(db,source,parcels):
    if not parcels:return []
    return delivery.reconcile(db,source,parcels=sorted(parcels))


def _rows(db,table):
    return [dict(r) for r in db.execute(f'SELECT * FROM {table}')]


def first(path,file):
    source,payload=capture(path,'printer_spool',file)
    db=connect(path);results=[]
    try:
        with db:
            seen_paper={}
            seen_parcel={}
            for row in payload.get('rows',[]):
                key=str(row['id']);paper=str(row['paper_no']);parcel=str(row['parcel'])
                reservation=db.execute('SELECT status FROM printer_reservation WHERE id=?',(key,)).fetchone()
                resource_required=bool(db.execute('SELECT 1 FROM printer_reservation LIMIT 1').fetchone())
                existing=db.execute('SELECT * FROM print_copy WHERE id=?',(key,)).fetchone()
                conflict=(paper in seen_paper and seen_paper[paper]!=parcel)
                conflict|=bool(db.execute("SELECT 1 FROM print_copy WHERE json_extract(body,'$.paper_no')=? AND id<>?",(paper,key)).fetchone())
                duplicate=parcel in seen_parcel or bool(db.execute("SELECT 1 FROM print_copy WHERE json_extract(body,'$.parcel')=? AND status='有效' AND id<>?",(parcel,key)).fetchone())
                if resource_required and (not reservation or reservation['status'] not in ('已预留','已消耗')):status,reason='待资源','printer_reservation_missing'
                elif conflict:status,reason='冲突','paper_reused'
                elif duplicate:status,reason='冲突','multiple_active_copies'
                elif row.get('template')!=row.get('required_template'):status,reason='待补印','template_mismatch'
                elif not row.get('printed'):status,reason='待补印','receipt_missing'
                else:status,reason='有效','printed_and_unique'
                if existing and existing['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):
                    status,reason='冲突','copy_identity_conflict'
                else:
                    save(db,'print_copy',key,source,payload['version'],row,status,reason)
                if reservation:
                    from .resources import complete
                    complete(db,key,bool(row.get('printed')))
                if conflict:
                    db.execute("UPDATE print_copy SET status='冲突',reason='paper_reused' WHERE json_extract(body,'$.paper_no')=?",(paper,))
                seen_paper[paper]=parcel;seen_parcel[parcel]=key
                results.append(decide(db,'面单印制',key,status,reason,source))
            batch_id=f"batch:{payload['version']}:{source}"
            save(db,'print_batch',batch_id,source,payload['version'],{'copies':[r['subject'] for r in results]},
                 '有效' if results and all(r['status']=='有效' for r in results) else '待核','copy_rollup')
        return results
    finally:db.close()


def second(path,file):
    source,payload=capture(path,'reprint_request',file)
    db=connect(path);results=[];affected=set()
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);old=db.execute('SELECT * FROM print_copy WHERE id=?',(str(row['old_copy']),)).fetchone()
                repeated=db.execute('SELECT * FROM reprint_request WHERE id=?',(key,)).fetchone()
                if repeated and repeated['body']==json.dumps(row,sort_keys=True,ensure_ascii=False):
                    results.append(decide(db,'重印许可',key,repeated['status'],repeated['reason'],source))
                    continue
                at=datetime.fromisoformat(row['at']);expires=datetime.fromisoformat(row['expires'])
                if not old:status,reason='拒绝','old_copy_missing'
                elif old['status']!='有效':status,reason='拒绝','old_copy_not_active'
                elif row.get('reason') not in ('lost','damaged'):status,reason='拒绝','unsupported_reason'
                elif at>expires or not row.get('approver'):status,reason='拒绝','authorization_invalid'
                elif db.execute('SELECT 1 FROM reprint_grant WHERE json_extract(body,\'$.old_copy\')=? AND status=\'批准\'',(str(row['old_copy']),)).fetchone():status,reason='拒绝','already_reprinted'
                elif db.execute('SELECT 1 FROM print_copy WHERE id=?',(str(row['new_copy']),)).fetchone():status,reason='拒绝','replacement_identity_conflict'
                elif db.execute("SELECT 1 FROM print_copy WHERE json_extract(body,'$.paper_no')=?",(str(row['new_paper']),)).fetchone():status,reason='拒绝','paper_reused'
                elif db.execute('SELECT 1 FROM printer_reservation LIMIT 1').fetchone() and not db.execute("SELECT 1 FROM printer_reservation WHERE id=? AND status='已预留'",(str(row['new_copy']),)).fetchone():status,reason='待资源','reprint_resource_missing'
                else:status,reason='批准','authorized_reprint'
                save(db,'reprint_request',key,source,payload['version'],row,status,reason)
                grant={'old_copy':str(row['old_copy']),'new_copy':str(row.get('new_copy','')),'approver':row.get('approver')}
                save(db,'reprint_grant',f'grant:{key}',source,payload['version'],grant,status,reason)
                if status=='批准':
                    original=json.loads(old['body'])
                    affected.add(str(original['parcel']))
                    db.execute("UPDATE print_copy SET status='作废',reason='authorized_reprint' WHERE id=?",(old['id'],))
                    replacement={'id':str(row['new_copy']),'parcel':original['parcel'],'paper_no':str(row['new_paper']),
                                 'template':original['template'],'required_template':original['required_template'],'printed':True}
                    save(db,'print_copy',str(row['new_copy']),source,payload['version'],replacement,'有效','authorized_reprint')
                    from .resources import complete
                    complete(db,str(row['new_copy']),True)
                results.append(decide(db,'重印许可',key,status,reason,source))
            # A new label identity changes which proofs can be trusted.
            _reconcile(db,source,affected)
        return results
    finally:db.close()


def third(path,file):
    source,payload=capture(path,'carrier_handoff',file)
    db=connect(path);results=[];affected=set()
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);copy=db.execute('SELECT * FROM print_copy WHERE id=?',(str(row['copy']),)).fetchone()
                prior=db.execute('SELECT * FROM parcel WHERE id=?',(str(row['parcel']),)).fetchone()
                same_as_prior=bool(prior and prior['body']==json.dumps(row,sort_keys=True,ensure_ascii=False))
                # An authorized reprint supersedes the handed-off label, so the
                # parcel may travel again under its new identity.  Any other
                # second handoff of the same physical parcel is refused.
                rehandoff=False
                if prior and prior['status']=='已交接' and not same_as_prior:
                    prior_copy=str(json.loads(prior['body']).get('copy',''))
                    prior_row=db.execute('SELECT status FROM print_copy WHERE id=?',(prior_copy,)).fetchone()
                    already=db.execute("SELECT 1 FROM handoff WHERE status='已交接' AND "
                                       "json_extract(body,'$.parcel')=? AND json_extract(body,'$.copy')=? LIMIT 1",
                                       (str(row['parcel']),str(row['copy']))).fetchone()
                    rehandoff=bool(not already and prior_row and prior_row['status']!='有效'
                                   and copy and copy['status']=='有效'
                                   and json.loads(copy['body'])['parcel']==str(row['parcel']))
                if prior and prior['status']=='召回中':status,reason='拒交','recall_open'
                elif prior and prior['status']=='已交接' and not same_as_prior and not rehandoff:
                    status,reason='拒交','parcel_already_handed_off'
                elif not copy or copy['status']!='有效':status,reason='拒交','copy_not_valid'
                elif json.loads(copy['body'])['parcel']!=str(row['parcel']):status,reason='拒交','copy_parcel_mismatch'
                elif abs(float(row['actual_weight'])-float(row['declared_weight']))>float(row['tolerance']):status,reason='拒交','weight_mismatch'
                elif row.get('courier')!=row.get('ack_courier') or row.get('ack_copy')!=row.get('copy'):status,reason='待核','ack_mismatch'
                else:status,reason='已交接','carrier_verified_after_reprint' if rehandoff else 'carrier_verified'
                if not prior or prior['body']==json.dumps(row,sort_keys=True,ensure_ascii=False):
                    save(db,'parcel',str(row['parcel']),source,payload['version'],row,status,reason)
                save(db,'handoff',key,source,payload['version'],
                     {'parcel':row['parcel'],'copy':row['copy'],'courier':row.get('courier'),
                      'seal':row.get('seal'),'at':row.get('at')},status,reason)
                affected.add(str(row['parcel']))
                results.append(decide(db,'承运交接',key,status,reason,source))
            # Handoff identity and label status decide whether end proofs count.
            _reconcile(db,source,affected)
        return results
    finally:db.close()


def settle(path,file):
    source,payload=capture(path,'carrier_settlement',file)
    db=connect(path);results=[];affected=set()
    try:
        with db:
            fulfilment={x['parcel']:x for x in delivery.summarize(db)}
            for row in payload.get('rows',[]):
                key=str(row['id']);ids=[str(x) for x in row['handoffs']]
                previous=db.execute('SELECT * FROM handoff_batch WHERE id=?',(key,)).fetchone()
                if previous and previous['status']=='争议重开':
                    if previous['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):raise ValueError('settlement_identity_conflict')
                    results.append(decide(db,'承运交接',key,'争议重开','carrier_dispute',source));continue
                handoffs=[db.execute('SELECT * FROM handoff WHERE id=?',(hid,)).fetchone() for hid in ids]
                if not ids or len(set(ids))!=len(ids):status,reason='待核','invalid_scope'
                elif any(h is None for h in handoffs):status,reason='待核','handoff_missing'
                elif any(h['status']!='已交接' for h in handoffs):status,reason='部分失败','handoff_not_complete'
                else:status,reason='已结算','all_handoffs_confirmed'
                save(db,'handoff_batch',key,source,payload['version'],row,status,reason)
                for hid,h in zip(ids,handoffs):
                    parcel=str(json.loads(h['body'])['parcel']) if h else ''
                    state=fulfilment.get(parcel,{})
                    save(db,'settlement_item',f'{key}:{hid}',source,payload['version'],
                         {'handoff':hid,'handoff_status':h['status'] if h else '缺失',
                          'parcel':parcel or None,
                          'fulfilment':state.get('status','无末端证据'),
                          'fulfilment_reason':state.get('reason')},status,reason)
                    if h:affected.add(str(json.loads(h['body'])['parcel']))
                results.append(decide(db,'承运交接',key,status,reason,source))
            # Settlement remains a historical fact; it never freezes end proofs.
            _reconcile(db,source,affected)
        return results
    finally:db.close()


def demo(root):
    db=root/'ledger.db'
    from .resources import plan
    resource=root/'printers.json'
    resource.write_text(json.dumps({'version':'v1','printers':[{'id':'P1','templates':['T1'],'paper':{'A6':10},'capacity':2,'online':True}],
                                    'jobs':[{'copy_id':'C1','template':'T1','paper':'A6'},
                                            {'copy_id':'C2','template':'T1','paper':'A6'}]}),encoding='utf-8')
    plan(db,resource)
    files=[{'version':'v1','rows':[{'id':'C1','parcel':'P1','paper_no':'N1','template':'T1','required_template':'T1','printed':True}]},
           {'version':'v1','rows':[{'id':'R1','old_copy':'C1','reason':'damaged','approver':'supervisor',
                                    'at':'2026-01-01T10:00:00','expires':'2026-01-02T00:00:00',
                                    'new_copy':'C2','new_paper':'N2'}]},
           {'version':'v1','rows':[{'id':'H1','parcel':'P1','copy':'C2','actual_weight':1.0,
                                    'declared_weight':1.0,'tolerance':0.1,'courier':'K','ack_courier':'K','ack_copy':'C2'}]}]
    out=[]
    for i,(fn,obj) in enumerate(zip((first,second,third),files)):
        p=root/f'export{i}.json';p.write_text(json.dumps(obj),encoding='utf-8');out.append(fn(db,p))
    p=root/'settlement.json';p.write_text(json.dumps({'version':'v1','rows':[{'id':'B1','handoffs':['H1']}]}),encoding='utf-8')
    out.append(settle(db,p))
    return out
