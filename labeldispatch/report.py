import hashlib,json
from pathlib import Path
from .store import connect,TABLES
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
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        data['billing']=[]
        for recon in db.execute('SELECT * FROM bill_reconciliation ORDER BY id'):
            body=json.loads(recon['body'])
            paid=db.execute("SELECT COALESCE(SUM(json_extract(body,'$.amount_cents')),0) FROM payment_voucher "
                            "WHERE json_extract(body,'$.bill_source')=?",(body['bill_source'],)).fetchone()[0]
            claw=db.execute("SELECT COALESCE(SUM(json_extract(body,'$.amount_cents')),0) FROM clawback "
                            "WHERE json_extract(body,'$.bill_source')=?",(body['bill_source'],)).fetchone()[0]
            data['billing'].append({'bill_source':body['bill_source'],'status':recon['status'],
                                    'reason':recon['reason'],'complete':body['complete'],
                                    'payable':body['payable'],'paid_cents':int(paid),
                                    'clawback_cents':int(claw),
                                    'references_applied':body['references_applied']})
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
