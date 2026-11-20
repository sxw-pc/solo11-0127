import hashlib,json
from pathlib import Path
from .store import connect,TABLES
from . import delivery


def _source_summary(db,source_ids):
    out=[]
    for source_id in sorted(source_ids):
        if source_id is None:continue
        row=db.execute('SELECT * FROM export_file WHERE id=?',(source_id,)).fetchone()
        if not row:continue
        out.append({'id':row['id'],'kind':row['kind'],'version':row['version'],'hash':row['hash'],
                    'verified':hashlib.sha256(row['body']).hexdigest()==row['hash']})
    return out


def fulfilment(db):
    """Per-parcel answer: fulfilled or still to redeliver, and why."""
    parcels=[]
    for item in delivery.summarize(db):
        auth=item['authorization_detail'] or {}
        parcels.append({
            'parcel':item['parcel'],
            'status':item['status'],
            'reason':item['reason'],
            'service':item['service'] or auth.get('service'),
            'authorization':item['authorization'],
            'authorization_scope':{k:auth.get(k) for k in ('service','address','place','recipient_confirmed')
                                   if auth.get(k) is not None},
            'handoff':item['handoff'],
            'handoff_status':item['handoff_status'],
            'handoff_count':item['handoff_count'],
            'cycle':item['cycle'],
            'attempts':[{k:x[k] for k in ('attempt','at','service','status','reason','copy','courier',
                                          'seal','location','photo','photo_location','gps','signed')}
                        for x in item['attempts']],
            'conflicts':item['conflicts'],
            'conflict_detail':item['conflict_detail'],
            'sources':_source_summary(db,item['source_ids'])})
    return parcels


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
        data['fulfilment']=fulfilment(db)
        data['awaiting_redelivery']=[x['parcel'] for x in data['fulfilment'] if x['status']!='已履约']
        data['fulfilled']=[x['parcel'] for x in data['fulfilment'] if x['status']=='已履约']
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
