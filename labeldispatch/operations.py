"""Physical label copies, reprint authority, and carrier handoff.

A queue row with ``printed=true`` is only a claim by the dispatch software.  In
authenticated mode a copy becomes valid only when that claim is backed by

* an authenticated printer receipt bound to the printer, copy, paper number,
  template and the digest of the queue export, or
* two independent on-site operator scans of the same paper number for the same
  copy (the device-offline contract).

Without authentication configuration the legacy queue-claim behaviour is kept
but every decision is explicitly labelled with the ``legacy`` evidence level.
"""
import json
import os
from datetime import datetime
from .ingest import capture,capture_receipts,capture_witnesses
from .store import connect,save,put,decide

VALID_COPY_STATUSES=('有效',)
INACTIVE_COPY_STATUSES=('作废',)
ACCEPTED_RESULTS=('printed','ok')


def _rows(db,table):
    return [dict(r) for r in db.execute(f'SELECT * FROM {table}')]


def authenticated_mode(keys=None):
    """Authentication is on when a key source is supplied or configured."""
    return keys is not None or bool(os.environ.get('LABELDISPATCH_KEYS'))


def _reservation(db,copy_id):
    return db.execute('SELECT * FROM printer_reservation WHERE id=?',(str(copy_id),)).fetchone()


def _queue_digest(db,source_id):
    row=db.execute('SELECT hash FROM export_file WHERE id=?',(source_id,)).fetchone()
    return row['hash'] if row else None


def _paper_used_elsewhere(db,copy_id,paper):
    others=db.execute("SELECT id,status FROM print_copy WHERE json_extract(body,'$.paper_no')=? AND id<>?",
                      (str(paper),str(copy_id))).fetchall()
    for row in others:
        if row['status'] not in INACTIVE_COPY_STATUSES:
            return row['id']
    consumed=db.execute("SELECT id FROM paper_consumption WHERE json_extract(body,'$.paper_no')=? "
                        "AND json_extract(body,'$.copy_id')<>? LIMIT 1",(str(paper),str(copy_id))).fetchone()
    return consumed['id'] if consumed else None


def _binding_error(message,row,reservation,queue_digest):
    """Why a signed receipt does not bind to this copy, or ``None``.

    Reprint replacements are not queue output: their receipt may cite the
    export that proved physical output of the original, so the queue digest is
    only enforced for copies that came from a printer queue export.
    """
    if queue_digest and row.get('origin')!='reprint' and str(message.get('export_digest'))!=str(queue_digest):
        return 'receipt_export_mismatch'
    if str(message.get('paper_no'))!=str(row.get('paper_no')):
        return 'receipt_paper_mismatch'
    if str(message.get('template'))!=str(row.get('template')):
        return 'receipt_template_mismatch'
    if reservation:
        body=json.loads(reservation['body'])
        if body.get('printer') and str(message.get('printer'))!=str(body['printer']):
            return 'receipt_printer_mismatch'
        if body.get('template') and str(message.get('template'))!=str(body['template']):
            return 'receipt_template_mismatch'
        if message.get('paper') is not None and body.get('paper') is not None and str(message['paper'])!=str(body['paper']):
            return 'receipt_stock_paper_mismatch'
    if message.get('result') not in ACCEPTED_RESULTS:
        return 'receipt_result_not_printed'
    return None


def _witness_state(db,copy_id,row,queue_digest,paper):
    witnesses=[dict(w) for w in db.execute("SELECT * FROM physical_witness WHERE json_extract(body,'$.copy_id')=?",
                                           (str(copy_id),))]
    operators=set();by_operator={};conflict=False
    for record in witnesses:
        if record['status']!='已记录':continue
        body=json.loads(record['body'])
        if str(body.get('paper_no'))!=str(paper):conflict=True;continue
        if body.get('export_digest') and queue_digest and str(body['export_digest'])!=str(queue_digest):
            conflict=True;continue
        operator=str(body.get('operator'))
        operators.add(operator)
        by_operator.setdefault(operator,set()).add(str(body.get('paper_no')))
    # One operator observing two different paper numbers cannot be a witness.
    if any(len(papers)>1 for papers in by_operator.values()):conflict=True
    return {'ids':[w['id'] for w in witnesses],'operators':operators,
            'complete':len(operators)>=2,'conflict':conflict,'present':bool(witnesses)}


def evidence_verdict(db,copy_id,row,reservation,queue_digest):
    """Resolve the trusted physical-output evidence for one copy."""
    copy_id=str(copy_id);paper=str(row.get('paper_no',''))
    used_by=_paper_used_elsewhere(db,copy_id,paper)
    receipts=[dict(r) for r in db.execute("SELECT * FROM printer_receipt WHERE json_extract(body,'$.copy_id')=?",
                                          (copy_id,))]
    accepted=[r for r in receipts if r['status']=='已认证']
    rejected=[r for r in receipts if r['status']!='已认证']
    bound=[];contradiction=None
    for record in accepted:
        message=json.loads(record['body'])
        error=_binding_error(message,row,reservation,queue_digest)
        if error is None:bound.append(record['id'])
        elif contradiction is None:contradiction=error
    witness=_witness_state(db,copy_id,row,queue_digest,paper)
    base={'copy_id':copy_id,'queue_digest':queue_digest,'paper_no':paper,
          'template':str(row.get('template','')),'printer':None,
          'receipt_ids':[r['id'] for r in receipts],
          'accepted_receipt_ids':bound,
          'witness_ids':witness['ids'],'witness_operators':sorted(witness['operators'])}
    if reservation:
        base['printer']=json.loads(reservation['body']).get('printer')
    if used_by:
        base.update(level='none',authorized=False,status='冲突',reason='paper_reused',
                    detail=f'paper already used by {used_by}')
        return base
    if contradiction:
        # Two authenticated messages that disagree mean at least one device
        # report is wrong; never let the convenient one win.
        base.update(level='device',authorized=False,status='待核',reason=contradiction,
                    detail='signed printer receipt does not match the reserved physical output')
        return base
    if bound:
        base.update(level='device',authorized=True,status='有效',reason='device_receipt_verified',
                    detail='printer receipt signed by an active key binds printer, copy, paper, template and export')
        return base
    if witness['conflict']:
        base.update(level='witness',authorized=False,status='冲突',reason='witness_paper_conflict',
                    detail='operator scans disagree on paper number or export digest')
        return base
    if witness['complete']:
        base.update(level='witness',authorized=True,status='有效',reason='dual_witness_verified',
                    detail='two independent operators recorded the same paper number and copy')
        return base
    if witness['present']:
        base.update(level='witness',authorized=False,status='待核',reason='witness_incomplete',
                    detail='a second independent operator scan is required')
        return base
    if rejected:
        base.update(level='none',authorized=False,status='待核',reason='receipt_'+str(rejected[0]['reason']),
                    detail='queue claim has no authenticatable printer receipt')
        return base
    base.update(level='none',authorized=False,status='待核',reason='receipt_missing',
                detail='queue claims printed but no receipt or dual witness exists')
    return base


def _write_evidence(db,copy_id,verdict,source_id,version):
    put(db,'print_evidence',f'evidence:{copy_id}',source_id,version,verdict,verdict['status'],verdict['reason'])


def _apply_verdict(db,copy_id,source_id,version,row,status,reason,verdict):
    if verdict.get('level')=='legacy':
        # Legacy mode keeps the original content-addressed semantics, including
        # refusing to overwrite a captured copy body with a different one.
        save(db,'print_copy',str(copy_id),source_id,version,row,status,reason)
    else:
        put(db,'print_copy',str(copy_id),source_id,version,row,status,reason)
    _write_evidence(db,copy_id,verdict,source_id,version)
    from .resources import complete
    complete(db,str(copy_id),status=='有效',evidence=verdict)


def _legacy_verdict(copy_id,row,status,reason):
    return {'copy_id':str(copy_id),'queue_digest':None,'paper_no':str(row.get('paper_no','')),
            'template':str(row.get('template','')),'printer':None,
            'receipt_ids':[],'accepted_receipt_ids':[],'witness_ids':[],'witness_operators':[],
            'level':'legacy','authorized':status=='有效','status':status,'reason':reason,
            'detail':'unsigned queue exported printed flag; not authenticated physical output'}


def _queue_gate(db,row,reservation):
    """Queue-side preconditions that physical evidence cannot lift."""
    if row.get('template') is not None and row.get('required_template') is not None \
            and row.get('template')!=row.get('required_template'):
        return '待补印','template_mismatch'
    if row.get('origin')!='reprint' and not row.get('printed'):
        return '待补印','receipt_missing'
    if db.execute('SELECT 1 FROM printer_reservation LIMIT 1').fetchone():
        if not reservation or reservation['status'] not in ('已预留','已消耗'):
            return '待资源','printer_reservation_missing'
    return None


def _paper_reuse_ahead(db,payload,key,paper,parcel):
    """True when this paper number also appears on another parcel in the batch."""
    for other in payload.get('rows',[]):
        if str(other.get('id'))==str(key):continue
        if str(other.get('paper_no'))==str(paper) and str(other.get('parcel'))!=str(parcel):
            return True
    return False


def first(path,file,keys=None):
    if authenticated_mode(keys):
        # Fail fast on a broken key file before anything is captured.
        from .ingest import load_keys
        load_keys(keys)
    source,payload=capture(path,'printer_spool',file)
    auth=authenticated_mode(keys)
    db=connect(path);results=[]
    try:
        with db:
            queue_digest=_queue_digest(db,source)
            seen_paper={}
            seen_parcel={}
            conflicts=set()
            batch_ids=[]
            for row in payload.get('rows',[]):
                key=str(row['id']);paper=str(row['paper_no']);parcel=str(row['parcel'])
                batch_ids.append(key)
                reservation=_reservation(db,key)
                resource_required=bool(db.execute('SELECT 1 FROM printer_reservation LIMIT 1').fetchone())
                existing=db.execute('SELECT * FROM print_copy WHERE id=?',(key,)).fetchone()
                conflict=(paper in seen_paper and seen_paper[paper]!=parcel)
                conflict|=bool(db.execute("SELECT 1 FROM print_copy WHERE json_extract(body,'$.paper_no')=? AND id<>?",
                                          (paper,key)).fetchone())
                if auth and _paper_reuse_ahead(db,payload,key,paper,parcel):
                    # Never let the first copy consume paper that the same export
                    # also assigns to a different parcel.
                    conflict=True
                duplicate=parcel in seen_parcel or bool(db.execute(
                    "SELECT 1 FROM print_copy WHERE json_extract(body,'$.parcel')=? AND status='有效' AND id<>?",
                    (parcel,key)).fetchone())
                # Queue-side blockers are judged first; physical evidence can
                # never lift them and they never consume paper.
                if resource_required and (not reservation or reservation['status'] not in ('已预留','已消耗')):
                    status,reason='待资源','printer_reservation_missing'
                elif conflict:
                    status,reason='冲突','paper_reused'
                elif duplicate:
                    status,reason='冲突','multiple_active_copies'
                elif row.get('template')!=row.get('required_template'):
                    status,reason='待补印','template_mismatch'
                elif not row.get('printed') and row.get('origin')!='reprint':
                    status,reason='待补印','receipt_missing'
                elif auth:
                    verdict=evidence_verdict(db,key,row,reservation,queue_digest)
                    status,reason=verdict['status'],verdict['reason']
                else:
                    status,reason='有效','printed_and_unique'
                identity_conflict=False
                if existing:
                    prior=json.loads(existing['body'])
                    if auth:
                        same_identity=(str(prior.get('parcel'))==parcel and str(prior.get('paper_no'))==paper)
                        identity_conflict=not same_identity
                    else:
                        identity_conflict=existing['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False)
                    if identity_conflict:
                        status,reason='冲突','copy_identity_conflict'
                if auth:
                    if identity_conflict:
                        # Keep the previously captured copy body; only flag it.
                        verdict=dict(evidence_verdict(db,key,row,reservation,queue_digest),
                                     authorized=False,status=status,reason=reason,
                                     detail='queue output for this copy id contradicts earlier capture')
                        _write_evidence(db,key,verdict,source,payload['version'])
                        from .resources import complete
                        complete(db,key,False,evidence=verdict)
                    else:
                        if status in ('待资源','待补印','冲突'):
                            verdict=dict(evidence_verdict(db,key,row,reservation,queue_digest),
                                         authorized=False,status=status,reason=reason,
                                         detail='blocked by queue-side precondition')
                        else:
                            verdict=evidence_verdict(db,key,row,reservation,queue_digest)
                        _apply_verdict(db,key,source,payload['version'],row,status,reason,verdict)
                else:
                    verdict=_legacy_verdict(key,row,status,reason)
                    _apply_verdict(db,key,source,payload['version'],row,status,reason,verdict)
                if conflict or identity_conflict:
                    conflicts.add(paper)
                seen_paper[paper]=parcel;seen_parcel[parcel]=key
                results.append(decide(db,'面单印制',key,status,reason,source))
            for paper in conflicts:
                # Only copies in this export are re-flagged: an already settled
                # historical copy is challenged through the dispute path, not
                # by rewriting its ledger row from a later export.
                db.execute("UPDATE print_copy SET status='冲突',reason='paper_reused' WHERE json_extract(body,'$.paper_no')=? "
                           "AND id IN (%s)"%','.join('?'*len(batch_ids)) if batch_ids else "SELECT 1",
                           (paper,*batch_ids))
                rows=db.execute("SELECT id,body FROM print_copy WHERE json_extract(body,'$.paper_no')=? "
                                "AND id IN (%s)"%','.join('?'*len(batch_ids)) if batch_ids else "SELECT 1",
                                (paper,*batch_ids)).fetchall()
                for record in rows:
                    body=json.loads(record['body'])
                    if auth:
                        evidence=dict(_evidence_or_placeholder(db,record['id'],body),authorized=False,status='冲突',
                                      reason='paper_reused',detail='paper number reused by another copy')
                    else:
                        evidence=dict(_legacy_verdict(record['id'],body,'冲突','paper_reused'),level='legacy')
                    put(db,'print_evidence',f"evidence:{record['id']}",source,payload['version'],
                        evidence,'冲突','paper_reused')
                    from .resources import complete
                    complete(db,str(record['id']),False,evidence=evidence)
                    decided=decide(db,'面单印制',str(record['id']),'冲突','paper_reused',source)
                    if auth:
                        for item in results:
                            if item['subject']==str(record['id']):
                                item['status']='冲突';item['reason']='paper_reused'
                        if not any(item['subject']==str(record['id']) for item in results):
                            results.append(decided)
            batch_id=f"batch:{payload['version']}:{source}"
            save(db,'print_batch',batch_id,source,payload['version'],{'copies':[r['subject'] for r in results]},
                 '有效' if results and all(r['status']=='有效' for r in results) else '待核','copy_rollup')
        return results
    finally:db.close()


def _evidence_or_placeholder(db,copy_id,body):
    record=db.execute('SELECT body FROM print_evidence WHERE id=?',(f'evidence:{copy_id}',)).fetchone()
    if record:return json.loads(record['body'])
    return {'copy_id':str(copy_id),'queue_digest':None,'paper_no':str(body.get('paper_no','')),
            'template':str(body.get('template','')),'printer':None,'receipt_ids':[],
            'accepted_receipt_ids':[],'witness_ids':[],'witness_operators':[],
            'level':'none','authorized':False,'detail':''}


def _refresh_copy(db,copy_id):
    """Re-adjudicate one copy after new physical evidence is captured."""
    copy=db.execute('SELECT * FROM print_copy WHERE id=?',(str(copy_id),)).fetchone()
    if not copy:return None
    if copy['status']=='作废':
        # A voided original remains void; its replacement is judged separately.
        return None
    row=json.loads(copy['body'])
    reservation=_reservation(db,copy_id)
    queue_digest=_queue_digest(db,copy['source_id'])
    verdict=evidence_verdict(db,copy_id,row,reservation,queue_digest)
    handed_off=bool(db.execute("SELECT 1 FROM handoff WHERE json_extract(body,'$.copy')=? AND status='已交接'",
                               (str(copy_id),)).fetchone())
    if handed_off:
        # Handoff was decided on evidence already on file; a later receipt may
        # not silently re-open or upgrade it.
        if not verdict['authorized'] and copy['status']=='有效':
            db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                       (copy['source_id'],'post_handoff_evidence_conflict',f'print_copy:{copy_id}'))
        return verdict
    gate=_queue_gate(db,row,reservation)
    if gate:
        verdict=dict(verdict,authorized=False,status=gate[0],reason=gate[1],
                     detail='blocked by queue-side precondition')
    _apply_verdict(db,copy_id,copy['source_id'],copy['version'],row,verdict['status'],verdict['reason'],verdict)
    decide(db,'面单印制',str(copy_id),verdict['status'],verdict['reason'],copy['source_id'])
    return verdict


def receipts(path,file,keys=None):
    """Capture authenticated printer receipts and re-adjudicate affected copies."""
    source,payload,captured=capture_receipts(path,file,keys)
    db=connect(path);results=[]
    try:
        with db:
            for item in captured:
                if item['status']=='已认证':
                    _refresh_copy(db,item['copy_id'])
                results.append(item)
        return results
    finally:db.close()


def witnesses(path,file):
    """Capture on-site operator scans and re-adjudicate affected copies."""
    source,payload,captured=capture_witnesses(path,file)
    db=connect(path)
    try:
        with db:
            for item in captured:
                if item.get('copy_id'):
                    _refresh_copy(db,item['copy_id'])
        return captured
    finally:db.close()


def second(path,file,keys=None):
    source,payload=capture(path,'reprint_request',file)
    auth=authenticated_mode(keys)
    db=connect(path);results=[]
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
                elif db.execute("SELECT 1 FROM reprint_grant WHERE json_extract(body,'$.old_copy')=? AND status='批准'",
                                (str(row['old_copy']),)).fetchone():status,reason='拒绝','already_reprinted'
                elif db.execute('SELECT 1 FROM print_copy WHERE id=?',(str(row['new_copy']),)).fetchone():status,reason='拒绝','replacement_identity_conflict'
                elif db.execute("SELECT 1 FROM print_copy WHERE json_extract(body,'$.paper_no')=?",(str(row['new_paper']),)).fetchone():status,reason='拒绝','paper_reused'
                elif db.execute('SELECT 1 FROM printer_reservation LIMIT 1').fetchone() and not db.execute(
                        "SELECT 1 FROM printer_reservation WHERE id=? AND status='已预留'",(str(row['new_copy']),)).fetchone():
                    status,reason='待资源','reprint_resource_missing'
                else:status,reason='批准','authorized_reprint'
                save(db,'reprint_request',key,source,payload['version'],row,status,reason)
                grant={'old_copy':str(row['old_copy']),'new_copy':str(row.get('new_copy','')),'approver':row.get('approver')}
                save(db,'reprint_grant',f'grant:{key}',source,payload['version'],grant,status,reason)
                if status=='批准':
                    original=json.loads(old['body'])
                    replacement={'id':str(row['new_copy']),'parcel':original['parcel'],'paper_no':str(row['new_paper']),
                                 'template':original['template'],'required_template':original['required_template'],
                                 'origin':'reprint','printed':not auth}
                    db.execute("UPDATE print_copy SET status='作废',reason='authorized_reprint' WHERE id=?",(old['id'],))
                    if auth:
                        # Authorization is not physical output: wait for a receipt
                        # or dual witness before the replacement face sheet counts.
                        try:
                            put(db,'print_copy',str(row['new_copy']),source,payload['version'],replacement,
                                '待印','awaiting_physical_output')
                        except ValueError:
                            results.append(decide(db,'重印许可',key,'拒绝','replacement_identity_conflict',source))
                            continue
                        _write_evidence(db,str(row['new_copy']),
                                        {'copy_id':str(row['new_copy']),'queue_digest':None,
                                         'paper_no':str(row['new_paper']),'template':original.get('template'),
                                         'printer':None,'receipt_ids':[],'accepted_receipt_ids':[],
                                         'witness_ids':[],'witness_operators':[],
                                         'level':'none','authorized':False,'status':'待印',
                                         'reason':'awaiting_physical_output',
                                         'detail':'reprint authorized; physical output not yet evidenced'},
                                        source,payload['version'])
                    else:
                        save(db,'print_copy',str(row['new_copy']),source,payload['version'],replacement,'有效','authorized_reprint')
                        _write_evidence(db,str(row['new_copy']),
                                        _legacy_verdict(row['new_copy'],replacement,'有效','authorized_reprint'),
                                        source,payload['version'])
                        from .resources import complete
                        complete(db,str(row['new_copy']),True)
                results.append(decide(db,'重印许可',key,status,reason,source))
        return results
    finally:db.close()


def third(path,file):
    source,payload=capture(path,'carrier_handoff',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);copy=db.execute('SELECT * FROM print_copy WHERE id=?',(str(row['copy']),)).fetchone()
                prior=db.execute('SELECT * FROM parcel WHERE id=?',(str(row['parcel']),)).fetchone()
                if prior and prior['status']=='召回中':status,reason='拒交','recall_open'
                elif prior and prior['status']=='已交接' and prior['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):
                    status,reason='拒交','parcel_already_handed_off'
                elif not copy or copy['status']!='有效':status,reason='拒交','copy_not_valid'
                elif json.loads(copy['body'])['parcel']!=str(row['parcel']):status,reason='拒交','copy_parcel_mismatch'
                elif abs(float(row['actual_weight'])-float(row['declared_weight']))>float(row['tolerance']):status,reason='拒交','weight_mismatch'
                elif row.get('courier')!=row.get('ack_courier') or row.get('ack_copy')!=row.get('copy'):status,reason='待核','ack_mismatch'
                else:status,reason='已交接','carrier_verified'
                if not prior or prior['body']==json.dumps(row,sort_keys=True,ensure_ascii=False):
                    save(db,'parcel',str(row['parcel']),source,payload['version'],row,status,reason)
                save(db,'handoff',key,source,payload['version'],{'parcel':row['parcel'],'copy':row['copy'],'courier':row.get('courier')},status,reason)
                results.append(decide(db,'承运交接',key,status,reason,source))
        return results
    finally:db.close()


def settle(path,file):
    source,payload=capture(path,'carrier_settlement',file)
    db=connect(path);results=[]
    try:
        with db:
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
                    save(db,'settlement_item',f'{key}:{hid}',source,payload['version'],
                         {'handoff':hid,'handoff_status':h['status'] if h else '缺失'},status,reason)
                results.append(decide(db,'承运交接',key,status,reason,source))
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
