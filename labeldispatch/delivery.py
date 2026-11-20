"""Proof of delivery at the recipient's end.

A carrier handoff only says the parcel left the station.  This module turns
offline delivery attempts, the service authorization chosen at dispatch and
late receipts into a per-parcel fulfilment conclusion.  Every attempt keeps its
own physical outcome; nothing is overwritten by whichever file was uploaded
last.  Conclusions are keyed to the handoff cycle that was in force when the
evidence was produced, so a reprint or a new handoff starts a new cycle.
"""
import hashlib,json
from datetime import datetime
from .ingest import capture
from .store import connect,save,decide

SERVICES=('safe_place','face_to_face')
OUTCOME_ALIASES={
    'refused':'refused','拒收':'refused','returned':'returned','退回':'returned',
    'no_answer':'no_answer','空跑':'no_answer','undeliverable':'no_answer',
    'delivered':'delivered','已投递':'delivered','成功':'delivered',
}


def _rows(db,table):
    return [dict(r) for r in db.execute(f'SELECT * FROM {table}')]


def _body(row):
    try:return json.loads(row['body'])
    except (TypeError,ValueError):return {}


def _get(body,*names):
    for name in names:
        value=body.get(name)
        if value not in (None,''):return value
    return None


def _text(value):
    return str(value).strip() if value not in (None,'') else ''


def _parse_at(value):
    if value in (None,''):return None
    try:return datetime.fromisoformat(str(value))
    except (TypeError,ValueError):return None


def _ts(value):
    at=_parse_at(value)
    return at.timestamp() if at else None


def _before(a,b):
    ta,tb=_ts(a),_ts(b)
    return ta is not None and tb is not None and ta<tb


def _digest(value):
    raw=value if isinstance(value,str) else json.dumps(value,sort_keys=True,ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]


def _save_derived(db,table,key,source,version,body,status,reason):
    """Conclusions may be revised; they are not physical facts, so no
    identity conflict is raised when a later, better-founded pass changes them."""
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    db.execute(f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,version=excluded.version,'
               'body=excluded.body,status=excluded.status,reason=excluded.reason',
               (key,source,version,encoded,status,reason))


def _handoff_cycles(db):
    """All handoff rows per parcel in recording order.

    The operative handoff is the last accepted one; a rejected retry is kept
    for explanation but never becomes the handoff end proofs are measured
    against.  A newly accepted handoff starts a new delivery cycle.
    """
    cycles={}
    for row in db.execute('SELECT rowid,* FROM handoff ORDER BY rowid'):
        row=dict(row);parcel=_text(_body(row).get('parcel'))
        if parcel:cycles.setdefault(parcel,[]).append(row)
    return cycles


def _authorizations(db):
    by_parcel={}
    for row in _rows(db,'delivery_authorization'):
        parcel=_text(_body(row).get('parcel'))
        if parcel:by_parcel.setdefault(parcel,[]).append(row)
    return by_parcel


def _select_authorization(authorizations,at):
    """Pick the confirmed service authorization covering the attempt time."""
    if not authorizations:return None,['authorization_missing']
    covering=[];conflicts=[]
    authorizations=sorted(authorizations,key=lambda r:str(_get(_body(r),'valid_from','from') or ''))
    for row in authorizations:
        body=_body(row)
        if at is not None and _before(at,_get(body,'valid_from','from')):continue
        if at is not None and _before(_get(body,'valid_to','expires'),at):continue
        covering.append(row)
    if not covering:
        return None,['authorization_not_covering_attempt']
    if len(covering)>1:
        chosen=covering[-1]
        if len({_text(_body(x).get('service')) for x in covering})>1:
            conflicts.append('multiple_service_authorizations')
    else:
        chosen=covering[0]
    body=_body(chosen)
    if _text(body.get('service')) not in SERVICES:conflicts.append('unsupported_service')
    if body.get('recipient_confirmed') is not True and not _text(body.get('confirmed_by')):
        conflicts.append('authorization_unconfirmed')
    if not _text(_get(body,'address')):conflicts.append('authorization_address_missing')
    if _text(body.get('service'))=='safe_place' and not _text(_get(body,'place','safe_place')):
        conflicts.append('safe_place_not_specified')
    return chosen,conflicts


def _current_copies(db):
    copies={}
    for row in _rows(db,'print_copy'):
        copies[str(row['id'])]=dict(row,body=_body(row))
    return copies


def _usable_copy(copies,copy_id,parcel,handoff):
    copy=copies.get(copy_id)
    if not copy:return False,'copy_missing'
    if copy['status']!='有效':return False,'stale_label'
    if _text(copy['body'].get('parcel'))!=parcel:return False,'copy_parcel_mismatch'
    if handoff:
        bound=_text(_body(handoff).get('copy'))
        if bound and bound!=copy_id:return False,'copy_not_bound_to_handoff'
    return True,'copy_valid'


def _replayed_attempts(attempts):
    """A one-time code may be retried for one parcel but never prove two.

    Attempts are ordered by event time, so the earlier use stays valid and the
    later cross-parcel reuse is the one rejected as a replay.
    """
    replayed=set();usage={}
    for row in _sort_attempts(attempts):
        body=_body(row);code=_text(_get(body,'recipient_code','code','otp'))
        parcel=_text(body.get('parcel'))
        if not code or not parcel:continue
        if code in usage and usage[code]!=parcel:replayed.add(str(row['id']))
        else:usage.setdefault(code,parcel)
    return replayed


def _attempt_kind(body):
    raw=_get(body,'outcome','result','status')
    label=_text(raw)
    if label:
        return OUTCOME_ALIASES.get(label.lower(),OUTCOME_ALIASES.get(label,label.lower()))
    return 'delivered'


BLOCKING_AUTH={
    'authorization_missing','authorization_not_covering_attempt','authorization_unconfirmed',
    'authorization_address_missing','safe_place_not_specified','unsupported_service',
    'multiple_service_authorizations',
}


def assess_attempt(body,*,attempt_id,parcel,handoff,copies,authorization,
                   authorization_conflicts,replayed):
    """Return (status, reason, service, signed) for one physical attempt.

    '计为成功' requires the chosen service, the current label, the
    courier/seal identity and that service's physical proof to agree.  A GPS
    number or a photo digest alone never proves delivery.
    """
    kind=_attempt_kind(body)
    at=_get(body,'at','attempt_at','time')
    signed=False
    if kind=='refused':return '不计成功','recipient_refused','',signed
    if kind=='returned':return '不计成功','parcel_returned','',signed
    if kind=='no_answer':return '不计成功','no_answer','',signed
    if kind!='delivered':return '待补','unsupported_outcome','',signed
    if _parse_at(at) is None:return '待补','attempt_time_missing','',signed
    if not handoff:
        # Nothing was handed to a courier, so the parcel is still at the station;
        # an attempt export claiming otherwise cannot prove delivery.
        return '待补','handoff_missing','',signed
    if handoff['status']!='已交接':return '不计成功','handoff_not_final','',signed
    handoff_body=_body(handoff)
    if _before(at,_get(handoff_body,'at','handoff_at')):
        return '不计成功','attempt_before_handoff','',signed
    courier=_text(_get(body,'courier','carrier','courier_id'))
    if not courier:return '待补','courier_missing','',signed
    if _text(handoff_body.get('courier')) and courier!=_text(handoff_body.get('courier')):
        return '不计成功','courier_mismatch','',signed
    copy_id=_text(_get(body,'copy','copy_id','label'))
    if not copy_id:return '待补','copy_missing','',signed
    ok,why=_usable_copy(copies,copy_id,parcel,handoff)
    if not ok:return '不计成功',why,'',signed
    seal=_text(_get(body,'seal','seal_no','seal_id'))
    if not seal:return '待补','seal_missing','',signed
    if _text(handoff_body.get('seal')) and seal!=_text(handoff_body.get('seal')):
        return '不计成功','seal_mismatch','',signed
    auth_body=_body(authorization) if authorization else {}
    if _text(auth_body.get('seal_no')) and seal!=_text(auth_body.get('seal_no')):
        return '不计成功','seal_mismatch','',signed
    service=_text(auth_body.get('service')) if authorization else ''
    declared=_text(body.get('service'))
    if service and declared and declared!=service:
        return '不计成功','service_mismatch',service,signed
    blocking=[x for x in authorization_conflicts if x in BLOCKING_AUTH]
    if blocking:return '不计成功',blocking[0],service,signed
    location=_text(_get(body,'location','place','address'))
    gps=_get(body,'gps')
    if gps is None and _get(body,'gps_lat') is not None and _get(body,'gps_lng') is not None:
        gps=f"{_get(body,'gps_lat')},{_get(body,'gps_lng')}"
    photo=_text(_get(body,'door_photo','door_photo_hash','photo','photo_hash'))
    signer=_text(_get(body,'signer','recipient_signer','signed_by'))
    code=_text(_get(body,'recipient_code','code','otp'))
    if code and attempt_id in replayed:
        return '不计成功','code_replayed',service,signed
    if service=='face_to_face':
        # A photo, GPS fix or seal only shows somebody reached the door; the
        # one-time code checked against the recipient is what completes it.
        if not signer and not code:return '待补','recipient_check_missing',service,signed
        if not signer:return '待补','recipient_signature_missing',service,signed
        if not code:return '待补','code_missing_physical_check',service,signed
        signed=True
        return '计为成功','face_to_face_code_verified',service,signed
    if service=='safe_place':
        if not location:return '待补','location_missing',service,signed
        authorized=_text(_get(auth_body,'address'))
        photo_location=_text(_get(body,'photo_location','photo_address'))
        if location!=authorized or (photo_location and photo_location!=authorized):
            return '不计成功','location_mismatch',service,signed
        if not gps:return '待补','gps_missing',service,signed
        if body.get('gps_trusted') is False:return '不计成功','gps_untrusted',service,signed
        if not photo:return '待补','door_photo_missing',service,signed
        return '计为成功','safe_place_proof_verified',service,signed
    return '不计成功','authorization_missing',service,signed


def _attempts_by_parcel(db):
    result={}
    for row in _rows(db,'delivery_attempt'):
        parcel=_text(_body(row).get('parcel'))
        if parcel:result.setdefault(parcel,[]).append(row)
    return result


def _sort_attempts(rows):
    def key(row):
        timestamp=_ts(_get(_body(row),'at','attempt_at','time'))
        return (timestamp is None,timestamp or 0.0,str(row['id']))
    return sorted(rows,key=key)


def summarize(db,parcels=None):
    """Recompute the fulfilment state of every parcel from stored evidence.

    Read-only: returns one record per parcel with the service authorization
    used, every physical attempt outcome, the conflicts and the raw-export
    ancestry.  :func:`reconcile` persists the same conclusion.
    """
    handoffs=_handoff_cycles(db)
    authorizations=_authorizations(db)
    copies=_current_copies(db)
    attempts=_attempts_by_parcel(db)
    replayed=_replayed_attempts([row for rows in attempts.values() for row in rows])
    universe=set(handoffs)|set(attempts)|set(authorizations)
    if parcels is not None:universe&=set(parcels)
    output=[]
    for parcel in sorted(universe):
        history=handoffs.get(parcel,[])
        accepted=[row for row in history if row['status']=='已交接']
        handoff=accepted[-1] if accepted else (history[-1] if history else None)
        # A newly accepted handoff (e.g. after an authorized reprint) opens a
        # new cycle; proofs produced before it cannot close the current one.
        cycle_start=None
        if handoff and handoff['status']=='已交接':
            cycle_start=_get(_body(handoff),'at','handoff_at')
        rows=_sort_attempts(attempts.get(parcel,[]))
        state,reason='待追投','no_physical_attempt'
        evaluated=[];conflicts=[];service='';auth_id=None;auth_detail=None
        delivered_at=None
        handoff_usable=bool(handoff and handoff['status']=='已交接')
        if not handoff:
            state,reason='待追投','handoff_missing'
        elif not handoff_usable:
            state,reason='待追投','handoff_under_dispute' if handoff['status']=='争议' else 'handoff_not_final'
        for row in rows:
            body=_body(row)
            at=_get(body,'at','attempt_at','time')
            auth,auth_conflicts=_select_authorization(authorizations.get(parcel,[]),at)
            if auth:
                auth_id=str(auth['id']);auth_detail=_body(auth)
            if cycle_start and _before(at,cycle_start):
                status,why,attempt_service,signed='不计成功','attempt_before_current_cycle','',False
            else:
                status,why,attempt_service,signed=assess_attempt(
                    body,attempt_id=str(row['id']),parcel=parcel,handoff=handoff,copies=copies,
                    authorization=auth,authorization_conflicts=auth_conflicts,replayed=replayed)
            service=attempt_service or service
            kind=_attempt_kind(body)
            evaluated.append({'attempt':str(row['id']),'at':at,
                              'service':attempt_service,'status':status,'reason':why,
                              'copy':_text(_get(body,'copy','copy_id','label')),
                              'courier':_text(_get(body,'courier','carrier','courier_id')),
                              'seal':_text(_get(body,'seal','seal_no','seal_id')),
                              'location':_text(_get(body,'location','place','address')),
                              'photo':_text(_get(body,'door_photo','door_photo_hash','photo','photo_hash')),
                              'photo_location':_text(_get(body,'photo_location','photo_address')),
                              'gps':_get(body,'gps'),'signed':signed,'kind':kind,
                              'source_id':row['source_id']})
            if status!='计为成功' and why not in conflicts:conflicts.append(why)
            if not handoff_usable:
                # The handoff itself is disputed or missing; no attempt outcome
                # can lift the parcel out of redelivery.
                continue
            if status=='计为成功':
                # Last terminal event by event time wins: a later refusal or
                # return after a success puts the parcel back into redelivery.
                if delivered_at is None:delivered_at=at
                state,reason='已履约',why
            elif kind in ('refused','returned'):
                delivered_at=None
                state,reason='待追投',why
            elif delivered_at is None:
                state,reason='待追投',why
        source_ids={x['source_id'] for x in rows if x['source_id'] is not None}
        if handoff and handoff['source_id'] is not None:source_ids.add(handoff['source_id'])
        if auth_id:
            auth_row=db.execute('SELECT source_id FROM delivery_authorization WHERE id=?',(auth_id,)).fetchone()
            if auth_row and auth_row['source_id'] is not None:source_ids.add(auth_row['source_id'])
        output.append({'parcel':parcel,'status':state,'reason':reason,
                       'service':service,'authorization':auth_id,
                       'authorization_detail':auth_detail,
                       'handoff':str(handoff['id']) if handoff else None,
                       'handoff_status':handoff['status'] if handoff else '缺失',
                       'handoff_count':len(history),
                       'cycle':f'{parcel}:{handoff["id"] if handoff else "none"}',
                       'cycle_start':cycle_start,
                       'attempts':evaluated,'conflicts':conflicts,
                       'conflict_detail':{'attempts':[{'attempt':x['attempt'],'status':x['status'],
                                                       'reason':x['reason']}
                                                      for x in evaluated if x['status']!='计为成功']},
                       'evidence':[{'attempt':x['attempt'],'status':x['status'],'reason':x['reason']}
                                   for x in evaluated],
                       'source_ids':sorted(source_ids)})
    return output


def confirm(path,file):
    """Import the service chosen at dispatch and the recipient's confirmation."""
    source,payload=capture(path,'delivery_authorization',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);body=dict(row)
                service=_text(body.get('service'))
                if service not in SERVICES:status,reason='拒绝','unsupported_service'
                elif body.get('recipient_confirmed') is not True and not _text(body.get('confirmed_by')):
                    status,reason='待核','authorization_unconfirmed'
                elif not _text(_get(body,'address')):status,reason='待核','authorization_address_missing'
                elif service=='safe_place' and not _text(_get(body,'place','safe_place')):
                    status,reason='待核','safe_place_not_specified'
                elif _before(_get(body,'valid_to','expires'),_get(body,'valid_from','from')):
                    status,reason='拒绝','authorization_window_invalid'
                else:status,reason='有效','service_authorized'
                save(db,'delivery_authorization',key,source,payload['version'],body,status,reason)
                results.append(decide(db,'末端投递',key,status,reason,source))
            # A changed service choice immediately re-weighs existing proofs.
            reconcile(db,source)
        return results
    finally:db.close()


def evaluate(path,file):
    """Import offline delivery attempts and persist per-attempt outcomes and
    the resulting fulfilment conclusion."""
    source,payload=capture(path,'delivery_evidence',file)
    db=connect(path)
    try:
        with db:
            for i,row in enumerate(payload.get('rows',[]),1):
                key=str(row.get('id') or f'{source}:{i}');body=dict(row);body['id']=key
                parcel=_text(body.get('parcel'))
                if not parcel or not _text(_get(body,'copy','copy_id','label')):
                    status,reason='待补','attempt_identity_incomplete'
                else:status,reason='已采','attempt_captured'
                save(db,'delivery_attempt',key,source,payload['version'],body,status,reason)
        return reconcile(db,source)
    finally:db.close()


def reconcile(db,source=None,parcels=None):
    """Assess every parcel and persist outcomes, cycles and decisions.

    Physical attempts are immutable; conclusions are revised in place so the
    report always reflects all evidence seen so far, including late receipts.
    """
    results=summarize(db,parcels=parcels)
    with db:
        for item in results:
            parcel=item['parcel'];handoff_id=item['handoff'] or 'none'
            attempt_rows=[r for r in _rows(db,'delivery_attempt')
                          if _text(_body(r).get('parcel'))==parcel]
            _save_derived(db,'delivery_cycle',f'{parcel}:{handoff_id}',source,'reconciled',{
                'parcel':parcel,'handoff':item['handoff'],'service':item['service'],
                'authorization':item['authorization'],
                'attempts':sorted(str(r['id']) for r in attempt_rows)},
                '已履约' if item['status']=='已履约' else '待追投',item['reason'])
            for ev in item['attempts']:
                _save_derived(db,'delivery_outcome',ev['attempt'],source,'reconciled',
                     {'attempt':ev['attempt'],'parcel':parcel,'service':ev['service'],'at':ev['at'],
                      'physical_outcome':ev['kind'],'signed':ev['signed'],
                      'evidence':{k:ev[k] for k in ('copy','courier','seal','location','photo',
                                                    'photo_location','gps')}},
                     ev['status'],ev['reason'])
            fingerprint=_digest(sorted(str(r['id']) for r in attempt_rows))
            _save_derived(db,'delivery_assessment',f'{parcel}:{handoff_id}:{fingerprint}',source,'reconciled',{
                'parcel':parcel,'handoff':item['handoff'],'cycle':item['cycle'],
                'service':item['service'],'authorization':item['authorization'],
                'attempts':item['evidence'],'conflicts':item['conflicts']},
                item['status'],item['reason'])
            decide(db,'末端投递',parcel,item['status'],item['reason'],source)
    return results
