"""Content-addressed capture of raw desktop exports."""
import hashlib,json
from pathlib import Path
from .store import connect


def canonical(item):
    return json.dumps(item,sort_keys=True,ensure_ascii=False)


def capture_record(db,source_id,kind,line_no,identity,item):
    """Store one raw export row once; re-importing identical bytes adds nothing."""
    encoded=canonical(item)
    old=db.execute('SELECT * FROM export_record WHERE source_id=? AND line_no=?',(source_id,line_no)).fetchone()
    if old:
        if old['body']!=encoded:
            db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                       (source_id,'record_identity_conflict',f'{kind}:{identity}'))
            raise ValueError('record_identity_conflict')
        return old['id']
    return db.execute('INSERT INTO export_record(source_id,line_no,identity,body,status) VALUES (?,?,?,?,?)',
                      (source_id,line_no,identity,encoded,'captured')).lastrowid


def capture(path,kind,file):
    raw=Path(file).read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    payload=json.loads(raw)
    if not isinstance(payload,dict) or not isinstance(payload.get('version'),str) or not payload['version']:
        raise ValueError('version_required')
    db=connect(path)
    try:
        with db:
            old=db.execute('SELECT id FROM export_file WHERE kind=? AND hash=?',(kind,digest)).fetchone()
            if old:return old['id'],payload
            source_id=db.execute('INSERT INTO export_file(kind,version,hash,body) VALUES (?,?,?,?)',
                                 (kind,payload['version'],digest,raw)).lastrowid
            for i,item in enumerate(payload.get('rows',[]),1):
                identity=str(item.get('id',i))
                capture_record(db,source_id,kind,i,identity,item)
        return source_id,payload
    finally:db.close()
