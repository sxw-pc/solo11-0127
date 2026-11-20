import hashlib,json
from pathlib import Path
from .store import connect,TABLES
from .ingest import RECEIPT_KIND,WITNESS_KIND


def _evidence_rows(db,prefix):
    return [dict(x) for x in db.execute('SELECT * FROM print_evidence WHERE id LIKE ? ORDER BY id',(prefix+'%',))]


def _redact(row):
    """Reports never carry signatures or secrets; they keep the message body
    digest and key fingerprint so the evidence stays auditable."""
    body=json.loads(row['body'])
    if 'signature' in body:
        signature=body.pop('signature')
        body['signature_sha256']=hashlib.sha256(str(signature).encode('utf-8')).hexdigest()
        body['signature_redacted']=True
    row['body']=json.dumps(body,sort_keys=True,ensure_ascii=False)
    return row


def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')]}
        for table in TABLES:
            rows=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
            if table in ('printer_receipt','receipt_key'):
                rows=[_redact(x) for x in rows]
            data['entities'][table]=rows
        data['evidence']=_summarize(db)
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()


def _summarize(db):
    """Facesheet-level report: valid copies, pending reasons, receipt origin, stock."""
    valid=[];pending=[];superseded=[];origin={}
    for record in _evidence_rows(db,'evidence:'):
        body=json.loads(record['body'])
        copy_id=body.get('copy_id')
        copy=db.execute('SELECT status,reason FROM print_copy WHERE id=?',(str(copy_id),)).fetchone()
        # The ledger row is authoritative: evidence never resurrects a copy that
        # was later voided by an authorized reprint.
        status=copy['status'] if copy else body.get('status')
        reason=copy['reason'] if copy else body.get('reason')
        receipts=[]
        for rid in body.get('receipt_ids',[]):
            source=db.execute('SELECT r.status,r.reason,r.source_id,e.kind,e.hash FROM printer_receipt r '
                              'LEFT JOIN export_file e ON e.id=r.source_id WHERE r.id=?',(rid,)).fetchone()
            if source:
                receipts.append({'receipt_id':rid,'status':source['status'],'reason':source['reason'],
                                 'source_kind':source['kind'],'export_hash':source['hash']})
        witnesses=[]
        for wid in body.get('witness_ids',[]):
            source=db.execute('SELECT w.status,w.reason,w.source_id,e.kind,e.hash FROM physical_witness w '
                              'LEFT JOIN export_file e ON e.id=w.source_id WHERE w.id=?',(wid,)).fetchone()
            if source:
                witnesses.append({'witness_id':wid,'status':source['status'],'reason':source['reason'],
                                  'source_kind':source['kind'],'export_hash':source['hash']})
        entry={'copy_id':copy_id,'status':status,'reason':reason,
               'evidence_level':body.get('level'),'authorized':body.get('authorized') and status=='有效',
               'paper_no':body.get('paper_no'),'template':body.get('template'),
               'printer':body.get('printer'),'queue_export_hash':body.get('queue_digest'),
               'receipts':receipts,'witnesses':witnesses,'witness_operators':body.get('witness_operators',[])}
        origin[copy_id]={'evidence_level':body.get('level'),'receipt_export_hashes':sorted({r['export_hash'] for r in receipts if r.get('export_hash')}),
                         'witness_export_hashes':sorted({w['export_hash'] for w in witnesses if w.get('export_hash')}),
                         'receipt_ids':[r['receipt_id'] for r in receipts if r['status']=='已认证'],
                         'rejected_receipt_ids':[r['receipt_id'] for r in receipts if r['status']!='已认证']}
        if status=='有效' and entry['authorized']:
            valid.append(entry)
        elif status=='作废':
            superseded.append(entry)
        else:
            pending.append(entry)
    stock=[]
    consumed_by=db.execute("SELECT json_extract(body,'$.printer') p,json_extract(body,'$.paper') w,count(*) c "
                           "FROM paper_consumption WHERE status='已消耗' GROUP BY p,w").fetchall()
    consumed_map={(r['p'],r['w']):r['c'] for r in consumed_by}
    for record in db.execute('SELECT * FROM paper_stock ORDER BY id'):
        body=json.loads(record['body'])
        snapshot=body.get('available')
        used=consumed_map.get((body.get('printer'),body.get('paper')),0)
        stock.append({'stock_id':record['id'],'status':record['status'],'reason':record['reason'],
                      'printer':body.get('printer'),'paper':body.get('paper'),
                      'snapshot_available':snapshot,
                      'consumed':used,
                      'available':(int(snapshot)-used) if snapshot is not None else None})
    consumed=db.execute("SELECT count(*) FROM paper_consumption WHERE status='已消耗'").fetchone()[0]
    reservations={}
    for record in db.execute('SELECT status,count(*) c FROM printer_reservation GROUP BY status'):
        reservations[record['status']]=record['c']
    return {'valid_copies':valid,'pending_copies':pending,'superseded_copies':superseded,
            'receipt_origin':origin,'paper_stock':stock,'paper_consumed_copies':consumed,
            'reservations_by_status':reservations}
