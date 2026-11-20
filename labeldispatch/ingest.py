"""Capture of desktop exports and authenticated physical-output evidence.

Queue exports ("printed=true") are only a claim by the dispatch software.  A
printer receipt is a device-signed message binding the printer, copy, paper
number, template, monotonic device sequence and the digest of the queue export
that was printed.  Receipts are verified here, before operations may turn a
copy into a valid face sheet or consume paper.
"""
import hashlib,hmac,json,os
from pathlib import Path
from .store import connect,decide,put,save

RECEIPT_KIND='printer_receipt_export'
WITNESS_KIND='physical_witness_export'

# Fields a device message must carry.  The signature covers every field except
# ``signature`` itself, so none of these can be altered after signing.
RECEIPT_FIELDS=('receipt_id','printer','copy_id','paper_no','template','seq',
                'result','export_digest','key_id','signature')


def canonical(obj):
    return json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False)


def _canonical_bytes(obj):
    return canonical(obj).encode('utf-8')


def file_digest(file):
    return hashlib.sha256(Path(file).read_bytes()).hexdigest()


def receipt_signature(message,secret):
    """HMAC-SHA256 over the canonical message with ``signature`` removed."""
    payload={k:v for k,v in message.items() if k!='signature'}
    return hmac.new(str(secret).encode('utf-8'),_canonical_bytes(payload),hashlib.sha256).hexdigest()


def load_keys(source=None):
    """Load the runtime-controlled key file.

    ``source`` may be a path or an already-parsed mapping; otherwise the
    ``LABELDISPATCH_KEYS`` environment variable is used.  The repository only
    ever ships fictional test keys.  Parsed secrets stay in memory: only a
    fingerprint is written to the ledger and reports.
    """
    if source is None:
        source=os.environ.get('LABELDISPATCH_KEYS')
    if not source:
        raise ValueError('key_file_required')
    if isinstance(source,(str,os.PathLike)):
        try:
            data=json.loads(Path(source).read_text(encoding='utf-8'))
        except OSError as exc:
            raise ValueError('key_file_unreadable') from exc
        except ValueError as exc:
            raise ValueError('key_file_invalid') from exc
    else:
        data=source
    if not isinstance(data,dict):
        raise ValueError('key_file_invalid')
    printers={}
    for pid,spec in (data.get('printers') or {}).items():
        keys=spec.get('keys') or {}
        if not isinstance(keys,dict) or not keys:
            raise ValueError('key_file_invalid')
        active=spec.get('active')
        # The active version must be explicit; retained older versions exist
        # only to verify receipts that predate a rotation.
        if not active or active not in keys:
            raise ValueError('active_key_missing' if active else 'active_key_required')
        parsed={str(k):str(v) for k,v in keys.items()}
        for secret in parsed.values():
            if not secret:
                raise ValueError('key_file_invalid')
        printers[str(pid)]={'active':active,'keys':parsed}
    if not printers:
        raise ValueError('key_file_invalid')
    return printers


def key_fingerprint(secret):
    # A fingerprint identifies a key version without disclosing the secret.
    return hashlib.sha256(str(secret).encode('utf-8')).hexdigest()[:16]


def _new_source(db,kind,payload,raw,digest):
    old=db.execute('SELECT id FROM export_file WHERE kind=? AND hash=?',(kind,digest)).fetchone()
    if old:
        return old['id'],True
    source_id=db.execute('INSERT INTO export_file(kind,version,hash,body) VALUES (?,?,?,?)',
                         (kind,payload['version'],digest,raw)).lastrowid
    return source_id,False


def _valid_digest(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def _receipt_messages(payload):
    rows=payload.get('receipts')
    if rows is None:rows=payload.get('rows',[])
    return rows if isinstance(rows,list) else []


def _sequence_state(db,printer):
    """Highest accepted device sequence and the content known at that sequence."""
    rows=db.execute("SELECT id,body FROM printer_receipt WHERE status='已认证' AND json_extract(body,'$.printer')=?",
                    (str(printer),)).fetchall()
    max_seq=None;by_seq={}
    for row in rows:
        body=json.loads(row['body'])
        try:seq=int(body.get('seq'))
        except (TypeError,ValueError):continue
        by_seq.setdefault(seq,set()).add(row['id'])
        if max_seq is None or seq>max_seq:max_seq=seq
    return max_seq,by_seq


def _verify_message(message,printers):
    """Signature, key version and required-field checks (no ledger state).

    Any key version retained in the runtime key file can verify a receipt;
    whether a retired key may sign *new* output is decided against the device
    sequence in :func:`_ingest_receipt`.
    """
    if not isinstance(message,dict):
        return False,'receipt_invalid',None
    for field in RECEIPT_FIELDS:
        if field not in message or message[field] in (None,''):
            return False,'receipt_incomplete',None
    printer=str(message['printer']);spec=printers.get(printer)
    if spec is None:return False,'unknown_printer',None
    key_id=str(message['key_id'])
    if key_id not in spec['keys']:return False,'unknown_key_version',None
    secret=spec['keys'][key_id]
    try:seq=int(message['seq'])
    except (TypeError,ValueError):return False,'sequence_invalid',key_fingerprint(secret)
    if seq<0:return False,'sequence_invalid',key_fingerprint(secret)
    if not _valid_digest(message['export_digest']):return False,'export_digest_invalid',key_fingerprint(secret)
    if not isinstance(message['result'],str):return False,'result_invalid',key_fingerprint(secret)
    expected=receipt_signature(message,secret)
    if not hmac.compare_digest(expected,str(message['signature'])):
        return False,'bad_signature',key_fingerprint(secret)
    return True,'signed',key_fingerprint(secret)


def capture_receipts(path,file,keys=None):
    """Capture a printer receipt export, verifying every message before use.

    Returns ``(source_id, payload, results)``.  Rejected messages are retained
    as evidence with status ``拒绝``; they never authorize paper consumption.
    Unknown/retired key versions are only accepted as an idempotent replay of a
    receipt that was already accepted under that version.
    """
    printers=load_keys(keys)
    raw=Path(file).read_bytes();digest=hashlib.sha256(raw).hexdigest()
    payload=json.loads(raw)
    if not isinstance(payload,dict) or not isinstance(payload.get('version'),str) or not payload['version']:
        raise ValueError('version_required')
    db=connect(path);results=[]
    try:
        with db:
            source_id,existing=_new_source(db,RECEIPT_KIND,payload,raw,digest)
            if not existing:
                for i,item in enumerate(_receipt_messages(payload),1):
                    identity=str(item.get('receipt_id',i)) if isinstance(item,dict) else str(i)
                    db.execute('INSERT INTO export_record(source_id,line_no,identity,body,status) VALUES (?,?,?,?,?)',
                               (source_id,i,identity,json.dumps(item,sort_keys=True,ensure_ascii=False),'captured'))
            for item in _receipt_messages(payload):
                results.append(_ingest_receipt(db,item,printers,source_id,payload['version']))
        return source_id,payload,results
    finally:db.close()


def _ingest_receipt(db,message,printers,source_id,version):
    if not isinstance(message,dict):
        message={}
    receipt_id=None
    if message:
        receipt_id='receipt:'+hashlib.sha256(_canonical_bytes(message)).hexdigest()
    else:
        receipt_id='receipt:'+hashlib.sha256(b'').hexdigest()
    printer=str(message.get('printer',''))
    copy_id=str(message.get('copy_id',''))

    old=db.execute('SELECT * FROM printer_receipt WHERE id=?',(receipt_id,)).fetchone()
    if old:
        # Replay of a known receipt.  Its acceptance never changes, but the
        # stored message is still re-verified so a retired key version missing
        # from the runtime key file is reported rather than silently trusted.
        ok,verify_reason,_fp=_verify_message(message,printers)
        if not ok and verify_reason in ('unknown_key_version','unknown_printer','bad_signature','receipt_incomplete'):
            return {'receipt_id':receipt_id,'printer':printer,'copy_id':copy_id,
                    'status':'拒绝','reason':verify_reason,'source_id':old['source_id']}
        return {'receipt_id':receipt_id,'printer':printer,'copy_id':copy_id,
                'status':old['status'],'reason':old['reason'],'source_id':old['source_id']}

    ok,reason,fingerprint=_verify_message(message,printers)
    seq=None
    if ok:
        seq=int(message['seq'])
        previous,by_seq=_sequence_state(db,printer)
        spec=printers[str(message['printer'])]
        if seq in by_seq:
            ok,reason=False,'sequence_conflict'
        elif previous is not None and seq<=previous:
            ok,reason=False,'sequence_regression'
        elif spec.get('active') is not None and str(message['key_id'])!=spec['active']:
            # A retained old key verifies history; it may not sign new output.
            ok,reason=False,'key_version_not_active'
    if not ok:
        status='拒绝'
    else:
        status='已认证'
        reason='device_signed'
        spec=printers[str(message['printer'])]
        key_id=str(message['key_id'])
        key_body={'printer':str(message['printer']),'key_id':key_id,'fingerprint':fingerprint}
        key_status='当前密钥' if key_id==spec.get('active') else '历史密钥'
        save(db,'receipt_key',f"key:{message['printer']}:{key_id}",source_id,'key-registry',key_body,
             '已登记',key_status)
    save(db,'printer_receipt',receipt_id,source_id,version,message,status,reason)
    decide(db,'印机回执',receipt_id,status,reason,source_id)
    return {'receipt_id':receipt_id,'printer':printer,'copy_id':copy_id,'seq':seq,
            'status':status,'reason':reason,'source_id':source_id}


def capture_witnesses(path,file):
    """Capture on-site dual-operator physical scans of a paper number.

    Each row records one operator's independent observation.  Two distinct
    operators are required later; capture only retains the observations.
    """
    raw=Path(file).read_bytes();digest=hashlib.sha256(raw).hexdigest()
    payload=json.loads(raw)
    if not isinstance(payload,dict) or not isinstance(payload.get('version'),str) or not payload['version']:
        raise ValueError('version_required')
    rows=payload.get('witnesses',payload.get('rows',[]))
    if not isinstance(rows,list):rows=[]
    db=connect(path);results=[]
    try:
        with db:
            source_id,existing=_new_source(db,WITNESS_KIND,payload,raw,digest)
            if not existing:
                for i,item in enumerate(rows,1):
                    identity=str(item.get('id',i)) if isinstance(item,dict) else str(i)
                    db.execute('INSERT INTO export_record(source_id,line_no,identity,body,status) VALUES (?,?,?,?,?)',
                               (source_id,i,identity,json.dumps(item,sort_keys=True,ensure_ascii=False),'captured'))
            for item in rows:
                results.append(_ingest_witness(db,item,source_id,payload['version']))
        return source_id,payload,results
    finally:db.close()


def _ingest_witness(db,item,source_id,version):
    if not isinstance(item,dict):item={}
    witness_id='witness:'+hashlib.sha256(_canonical_bytes(item)).hexdigest()
    old=db.execute('SELECT * FROM physical_witness WHERE id=?',(witness_id,)).fetchone()
    if old:
        return {'witness_id':witness_id,'status':old['status'],'reason':old['reason'],
                'operator':json.loads(old['body']).get('operator'),'source_id':old['source_id']}
    missing=[f for f in ('copy_id','paper_no','operator','scan_ref') if not item.get(f)]
    if missing:
        status,reason='拒绝','witness_incomplete'
    elif item.get('export_digest') is not None and not _valid_digest(item.get('export_digest')):
        status,reason='拒绝','export_digest_invalid'
    else:
        status,reason='已记录','operator_scan_recorded'
    save(db,'physical_witness',witness_id,source_id,version,item,status,reason)
    decide(db,'现场见证',witness_id,status,reason,source_id)
    return {'witness_id':witness_id,'copy_id':str(item.get('copy_id','')),
            'paper_no':str(item.get('paper_no','')),'operator':item.get('operator'),
            'status':status,'reason':reason,'source_id':source_id}


def capture(path,kind,file):
    """Capture an unsigned desktop export (content addressed)."""
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
                db.execute('INSERT INTO export_record(source_id,line_no,identity,body,status) VALUES (?,?,?,?,?)',
                           (source_id,i,identity,json.dumps(item,sort_keys=True,ensure_ascii=False),'captured'))
        return source_id,payload
    finally:db.close()
