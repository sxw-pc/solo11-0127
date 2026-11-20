"""Printer routing and paper allocation before physical label creation."""
import json
from .ingest import capture
from .store import connect,save,put,decide


def plan(path,file):
    source,payload=capture(path,'printer_resource_export',file)
    db=connect(path);results=[]
    try:
        with db:
            version=payload['version']
            for printer in payload.get('printers',[]):
                save(db,'printer',f"{version}:{printer['id']}",source,version,printer,
                     '在线' if printer.get('online') else '离线','resource_snapshot')
                for paper,count in printer.get('paper',{}).items():
                    save(db,'paper_stock',f"{version}:{printer['id']}:{paper}",source,version,
                         {'printer':printer['id'],'paper':paper,'available':int(count)},'已登记','stock_snapshot')
            for job in payload.get('jobs',[]):
                key=str(job['copy_id'])
                old=db.execute('SELECT * FROM printer_reservation WHERE id=?',(key,)).fetchone()
                if old:
                    results.append(decide(db,'打印资源',key,old['status'],old['reason'],source))
                    continue
                selected=None
                for printer in sorted(payload.get('printers',[]),key=lambda x:x['id']):
                    if not printer.get('online') or job['template'] not in printer.get('templates',[]):continue
                    paper=job['paper'];available=int(printer.get('paper',{}).get(paper,0))
                    used=db.execute("SELECT count(*) FROM printer_reservation WHERE json_extract(body,'$.printer')=? AND json_extract(body,'$.paper')=? AND status IN ('已预留','已消耗')",
                                    (printer['id'],paper)).fetchone()[0]
                    total=db.execute("SELECT count(*) FROM printer_reservation WHERE json_extract(body,'$.printer')=? AND status='已预留'",(printer['id'],)).fetchone()[0]
                    if available>used and int(printer['capacity'])>total:
                        selected=printer;break
                if selected:status,reason='已预留','printer_and_paper_reserved'
                else:status,reason='待资源','printer_or_paper_unavailable'
                details=dict(job,printer=selected['id'] if selected else None)
                save(db,'printer_reservation',key,source,version,details,status,reason)
                results.append(decide(db,'打印资源',key,status,reason,source))
        return results
    finally:db.close()


def complete(db,copy_id,authorized,evidence=None):
    """Consume the paper reservation only for trusted physical output.

    ``authorized`` must come from the evidence chain (authenticated device
    receipt or complete dual-witness scans), never from a queue boolean.
    Consumption is recorded once per copy so replays and key rotation cannot
    count the same physical sheet twice.
    """
    key=str(copy_id)
    row=db.execute('SELECT * FROM printer_reservation WHERE id=?',(key,)).fetchone()
    if not row:return None
    if row['status']!='已预留' or not authorized:
        return row['status']
    body=json.loads(row['body'])
    printer=body.get('printer');stock_paper=body.get('paper')
    evidence=evidence or {}
    consumption_id=f'paper:{key}'
    old=db.execute('SELECT * FROM paper_consumption WHERE id=?',(consumption_id,)).fetchone()
    record={'copy_id':key,'printer':printer,'paper':stock_paper,
            'paper_no':evidence.get('paper_no'),'evidence_level':evidence.get('level'),
            'receipt_ids':evidence.get('accepted_receipt_ids',[]),
            'witness_ids':evidence.get('witness_ids',[]),
            'queue_digest':evidence.get('queue_digest')}
    if old:
        prior=json.loads(old['body'])
        if prior.get('paper_no')!=record.get('paper_no') or prior.get('printer')!=printer:
            db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                       (row['source_id'],'paper_consumption_conflict',consumption_id))
            return row['status']
        return row['status']
    # The stock snapshot is captured source data and is never rewritten.
    # Consumption is recorded separately; available stock is derived as
    # snapshot count minus accepted consumption records.
    stock=db.execute("SELECT * FROM paper_stock WHERE json_extract(body,'$.printer')=? "
                     "AND json_extract(body,'$.paper')=? ORDER BY id DESC",
                     (printer,stock_paper)).fetchall()
    if stock:
        record['stock_id']=stock[0]['id']
        record['snapshot_available']=int(json.loads(stock[0]['body']).get('available',0))
    put(db,'paper_consumption',consumption_id,row['source_id'],row['version'],record,'已消耗','paper_consumed')
    db.execute("UPDATE printer_reservation SET status='已消耗',reason='paper_consumed' WHERE id=?",(key,))
    return '已消耗'


def cancel(path,file):
    source,payload=capture(path,'printer_cancel_export',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['copy_id']);reservation=db.execute('SELECT * FROM printer_reservation WHERE id=?',(key,)).fetchone()
                if not reservation or reservation['status']!='已预留':status,reason='拒绝','reservation_not_open'
                elif not row.get('operator') or row.get('evidence')!='no_paper_output':status,reason='待核','physical_evidence_missing'
                else:
                    db.execute("UPDATE printer_reservation SET status='已释放',reason='operator_cancelled' WHERE id=?",(key,))
                    status,reason='已释放','operator_cancelled'
                results.append(decide(db,'打印资源',key,status,reason,source))
        return results
    finally:db.close()
