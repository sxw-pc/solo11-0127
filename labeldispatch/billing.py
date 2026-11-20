"""Carrier offline bill ingestion and payment reconciliation.

Bill lines only carry the fields the carrier actually exports (settlement batch,
receive date, service tier, billing window, amount in integer cents, optionally
a parcel id and a carrier-assigned line number).  Handoff records may likewise
lack the back-filled carrier line number.  Reconciliation therefore builds a
one-to-one candidate graph from those real fields only, enumerates every
feasible complete matching, and never lets one handoff carry two positive
charges inside a matching.  Payment is an actual decision with a voucher; later
disputes move money out of the payable part into a clawback balance instead of
rewriting historical payment evidence.
"""
import json
from .ingest import capture
from .store import connect, save, decide

ELIGIBLE_HANDOFF_STATUSES = ('已交接', '争议')
POLICIES = ('strict', 'lower_bound')
_MATCH_FIELDS = ('batch', 'handoff_date', 'service_tier', 'billing_window')


def _load_json(row):
    return json.loads(row['body'])


def _handoff_fields(handoff):
    body = _load_json(handoff)
    return {'id': handoff['id'], 'status': handoff['status'],
            'batch': body.get('batch'), 'handoff_date': body.get('handoff_date'),
            'service_tier': body.get('service_tier'), 'billing_window': body.get('billing_window'),
            'parcel': None if body.get('parcel') is None else str(body.get('parcel')),
            'carrier_line_no': body.get('carrier_line_no')}


def _compatible(line, handoff):
    """All shared, present fields must agree; absent fields never invent links."""
    for field in _MATCH_FIELDS:
        lv, hv = line.get(field), handoff.get(field)
        if lv is not None and hv is not None and str(lv) != str(hv):
            return False
    if line.get('parcel') is not None and handoff.get('parcel') is not None \
            and str(line['parcel']) != str(handoff['parcel']):
        return False
    if line.get('line_no') is not None and handoff.get('carrier_line_no') is not None \
            and str(line['line_no']) != str(handoff['carrier_line_no']):
        return False
    return True


def _enum_matchings(lines, edges):
    """All injective assignments covering every line (backtracking, deterministic)."""
    matchings = []
    assignment = {}
    used = set()

    def dfs(i):
        if i == len(lines):
            matchings.append(dict(assignment))
            return
        line_index = lines[i]['line_index']
        for handoff_id in edges.get(line_index, ()):
            if handoff_id in used:
                continue
            used.add(handoff_id)
            assignment[line_index] = handoff_id
            dfs(i + 1)
            used.discard(handoff_id)
            del assignment[line_index]

    dfs(0)
    return matchings


def _applied_references(db, source, batch, line_no):
    if line_no is None:
        return []
    return [r for r in db.execute(
        "SELECT * FROM bill_reference WHERE status='有效' AND "
        "json_extract(body,'$.source_digest')=(SELECT hash FROM export_file WHERE id=?) "
        "AND json_extract(body,'$.batch')=? AND json_extract(body,'$.carrier_line_no')=?",
        (source, batch, str(line_no)))]


def reconcile(db, source, version):
    """Rebuild candidate rows and payment bounds for one imported bill source."""
    bill_rows = [dict(r) for r in db.execute(
        'SELECT * FROM carrier_bill WHERE source_id=? ORDER BY id', (source,))]
    db.execute('DELETE FROM bill_candidate WHERE source_id=?', (source,))
    db.execute('DELETE FROM bill_reconciliation WHERE source_id=?', (source,))
    handoffs = [_handoff_fields(r) for r in db.execute('SELECT * FROM handoff ORDER BY id')]
    eligible = [h for h in handoffs if h['status'] in ELIGIBLE_HANDOFF_STATUSES]
    lines = []
    for row in bill_rows:
        body = _load_json(row)
        body['line_status'] = row['status']
        body['line_reason'] = row['reason']
        lines.append(body)

    per_batch = {}
    references_used = set()
    for line in lines:
        batch = line.get('batch')
        per_batch.setdefault(batch, []).append(line)
        refs = _applied_references(db, source, batch, line.get('line_no'))
        candidate_ids = []
        status, reason = line['line_status'], line['line_reason']
        if status == '有效':
            if refs:
                ref = refs[0]
                ref_body = _load_json(ref)
                references_used.add(ref_body['ref_source'])
                target = next((h for h in eligible if h['id'] == str(ref_body['handoff'])), None)
                if target is None or not _compatible(line, target):
                    status, reason = '冲突', 'reference_incompatible'
                else:
                    candidate_ids = [target['id']]
                    status, reason = '有效', 'reference_narrowed'
            else:
                candidate_ids = [h['id'] for h in eligible
                                 if h.get('batch') == batch and _compatible(line, h)]
                if not candidate_ids:
                    status, reason = '待核', 'no_feasible_handoff'
                else:
                    status, reason = '有效', 'field_candidate'
        cand_body = {'bill_source': source, 'line_id': line['line_id'], 'line_index': line['line_index'],
                     'line_no': line.get('line_no'), 'batch': batch,
                     'amount_cents': line.get('amount_cents'), 'candidates': candidate_ids,
                     'references': [json.loads(r['body'])['ref_source'] for r in refs]}
        save(db, 'bill_candidate', f"cand:{source}:{line['line_index']}", source, version,
             cand_body, status, reason)

    batches_out = {}
    complete_overall = True
    definite_lines, guaranteed_lines = [], []
    disputed_all = {h['id'] for h in eligible if h['status'] == '争议'}
    for batch, batch_lines in per_batch.items():
        blocked = [l['line_index'] for l in batch_lines if l['line_status'] != '有效']
        edges = {}
        for line in batch_lines:
            if line['line_status'] != '有效':
                continue
            cand = db.execute('SELECT body FROM bill_candidate WHERE id=?',
                              (f"cand:{source}:{line['line_index']}",)).fetchone()
            edges[line['line_index']] = json.loads(cand['body'])['candidates']
        valid_lines = [l for l in batch_lines if l['line_status'] == '有效']
        matchings = _enum_matchings(valid_lines, edges) if valid_lines and not blocked else []
        batch_complete = not blocked and bool(matchings)
        complete_overall &= batch_complete
        pay_per_matching = []
        for matching in matchings:
            total = sum(int(l['amount_cents']) for l in valid_lines
                        if matching[l['line_index']] not in disputed_all)
            pay_per_matching.append(total)
        definite = {}
        guaranteed = set()
        if matchings:
            for line in valid_lines:
                mapped = {m[line['line_index']] for m in matchings}
                if len(mapped) == 1:
                    handoff_id = next(iter(mapped))
                    definite[line['line_index']] = handoff_id
                    if handoff_id not in disputed_all:
                        definite_lines.append(line['line_id'])
            # A line is guaranteed only when its fee lands on a non-disputed
            # handoff in every feasible matching.
            for line in valid_lines:
                if all(m[line['line_index']] not in disputed_all for m in matchings):
                    guaranteed.add(line['line_index'])
                    guaranteed_lines.append(line['line_id'])
        batches_out[batch] = {'complete': batch_complete, 'blocked_lines': blocked,
                              'matching_count': len(matchings),
                              'matchings': [dict(sorted(m.items())) for m in matchings],
                              'definite': {str(k): v for k, v in definite.items()},
                              'guaranteed_lines': sorted(guaranteed),
                              'pay_per_matching': pay_per_matching}

    strict_payable = lower_payable = upper_payable = 0
    if complete_overall:
        strict_payable = sum(int(l['amount_cents']) for l in lines if l['line_id'] in definite_lines)
        # Matchings are chosen independently per batch, so the batch-level
        # worst/best payable totals add; flattening every matching into one
        # global min/max would mix choices from different batches.
        for batch_info in batches_out.values():
            pay_values = batch_info['pay_per_matching']
            if pay_values:
                lower_payable += min(pay_values)
                upper_payable += max(pay_values)

    recon_body = {'bill_source': source, 'complete': complete_overall,
                  'batches': {str(k): v for k, v in batches_out.items()},
                  'references_applied': sorted(references_used),
                  'payable': {'strict': strict_payable, 'lower_bound': lower_payable,
                              'upper_bound': upper_payable,
                              'definite_lines': definite_lines,
                              'guaranteed_lines': guaranteed_lines}}
    reason = 'complete_matching_exists' if complete_overall else 'no_complete_matching'
    save(db, 'bill_reconciliation', f'recon:{source}', source, version,
         recon_body, '已核对' if complete_overall else '待核', reason)
    return recon_body


def _same_bill_content(a, b):
    keys = ('batch', 'handoff_date', 'service_tier', 'billing_window', 'parcel', 'amount_cents')
    return all(a.get(k) == b.get(k) for k in keys)


def import_bill(path, file):
    source, payload = capture(path, 'carrier_bill_export', file)
    db = connect(path)
    results = []
    try:
        with db:
            raw_rows = payload.get('rows', [])
            seen_counts = {}
            for row in raw_rows:
                line_no = row.get('carrier_line_no')
                if line_no is not None:
                    key = (str(row.get('batch')), str(line_no))
                    seen_counts[key] = seen_counts.get(key, 0) + 1
            for i, row in enumerate(raw_rows, 1):
                line_id = f'bill:{source}:{i}'
                line_no = row.get('carrier_line_no')
                required = ('batch', 'handoff_date', 'service_tier', 'billing_window', 'amount_cents')
                missing = [k for k in required if row.get(k) is None]
                amount = row.get('amount_cents')
                amount_bad = isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0
                if missing:
                    status, reason = '待核', 'bill_field_missing'
                elif amount_bad:
                    status, reason = '待核', 'amount_invalid'
                elif line_no is not None and seen_counts[(str(row['batch']), str(line_no))] > 1:
                    status, reason = '冲突', 'duplicate_line_no'
                else:
                    status, reason = '有效', 'bill_line_captured'
                body = {'line_id': line_id, 'line_index': i, 'source_id': source,
                        'line_no': line_no, 'batch': row.get('batch'),
                        'handoff_date': row.get('handoff_date'),
                        'service_tier': row.get('service_tier'),
                        'billing_window': row.get('billing_window'),
                        'parcel': None if row.get('parcel') is None else str(row.get('parcel')),
                        'amount_cents': amount}
                if status == '有效' and line_no is not None:
                    prior = db.execute(
                        "SELECT * FROM carrier_bill WHERE source_id<>? AND "
                        "json_extract(body,'$.batch')=? AND json_extract(body,'$.line_no')=?",
                        (source, str(row['batch']), str(line_no))).fetchall()
                    for old in prior:
                        if not _same_bill_content(_load_json(old), body):
                            status, reason = '冲突', 'line_no_variant_conflict'
                            db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                                       (source, 'line_no_variant_conflict', f"{old['id']}->{line_id}"))
                            break
                save(db, 'carrier_bill', line_id, source, payload['version'], body, status, reason)
                results.append(decide(db, '承运账单', line_id, status, reason, source))
            reconcile(db, source, payload['version'])
        return results
    finally:
        db.close()


def add_reference(path, file):
    """Ingest a carrier line-number cross-reference; it only narrows candidates."""
    source, payload = capture(path, 'carrier_line_reference', file)
    db = connect(path)
    results = []
    try:
        with db:
            top_digest = payload.get('source_digest')
            affected = set()
            for i, row in enumerate(payload.get('rows', []), 1):
                ref_id = f'ref:{source}:{i}'
                digest = row.get('source_digest', top_digest)
                batch, line_no = row.get('batch'), row.get('carrier_line_no')
                handoff_id = None if row.get('handoff') is None else str(row['handoff'])
                body = {'ref_source': source, 'line_index': i, 'batch': batch,
                        'carrier_line_no': None if line_no is None else str(line_no),
                        'handoff': handoff_id, 'source_digest': digest}
                if not digest:
                    status, reason = '待核', 'source_digest_missing'
                else:
                    bill_source_row = db.execute(
                        'SELECT id FROM export_file WHERE kind=? AND hash=?',
                        ('carrier_bill_export', digest)).fetchone()
                    ok, why = (False, 'source_digest_mismatch') if bill_source_row is None else (True, 'verified')
                    if not ok:
                        status, reason = '待核', why
                    elif batch is None or line_no is None or handoff_id is None:
                        status, reason = '待核', 'reference_field_missing'
                    else:
                        bill_line = db.execute(
                            "SELECT * FROM carrier_bill WHERE source_id=? AND "
                            "json_extract(body,'$.batch')=? AND json_extract(body,'$.line_no')=?",
                            (bill_source_row['id'], str(batch), str(line_no))).fetchone()
                        if bill_line is None:
                            status, reason = '待核', 'bill_line_missing'
                        else:
                            handoff = db.execute('SELECT * FROM handoff WHERE id=?', (handoff_id,)).fetchone()
                            if handoff is None:
                                status, reason = '待核', 'handoff_missing'
                            elif handoff['status'] not in ELIGIBLE_HANDOFF_STATUSES:
                                status, reason = '待核', 'handoff_not_eligible'
                            elif not _compatible(_load_json(bill_line), _handoff_fields(handoff)):
                                status, reason = '冲突', 'reference_incompatible'
                            else:
                                variant = db.execute(
                                    "SELECT * FROM bill_reference WHERE status='有效' AND id<>? AND "
                                    "json_extract(body,'$.batch')=? AND json_extract(body,'$.carrier_line_no')=? "
                                    "AND json_extract(body,'$.handoff')<>?",
                                    (ref_id, str(batch), str(line_no), handoff_id)).fetchone()
                                if variant is not None:
                                    status, reason = '冲突', 'line_no_variant_conflict'
                                    db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                                               (source, 'line_no_variant_conflict',
                                                f"{variant['id']}->{ref_id}"))
                                else:
                                    status, reason = '有效', 'reference_applied'
                                    affected.add(bill_source_row['id'])
                save(db, 'bill_reference', ref_id, source, payload['version'], body, status, reason)
                results.append(decide(db, '账单对照', ref_id, status, reason, source))
            for bill_source in affected:
                version = db.execute('SELECT version FROM export_file WHERE id=?',
                                     (bill_source,)).fetchone()['version']
                reconcile(db, bill_source, version)
        return results
    finally:
        db.close()


def _previously_paid(db, source):
    total = db.execute(
        "SELECT COALESCE(SUM(json_extract(body,'$.amount_cents')),0) FROM payment_voucher "
        "WHERE json_extract(body,'$.bill_source')=?", (source,)).fetchone()[0]
    return int(total)


def _record_payment(db, source, policy, trigger_source, version):
    if policy not in POLICIES:
        raise ValueError('unknown_policy')
    recon_row = db.execute('SELECT * FROM bill_reconciliation WHERE source_id=?', (source,)).fetchone()
    if recon_row is None:
        raise ValueError('bill_not_reconciled')
    recon = _load_json(recon_row)
    payable = recon['payable'][policy] if recon['complete'] else 0
    paid_before = _previously_paid(db, source)
    pay_now = max(0, payable - paid_before)
    claw_now = max(0, paid_before - payable)
    seq = db.execute(
        "SELECT count(*) FROM payment_decision WHERE json_extract(body,'$.bill_source')=?",
        (source,)).fetchone()[0] + 1
    decision_id = f'pay:{source}:{seq}'
    if claw_now:
        status, reason = '追收', 'disputed_charge_moved_out'
    elif not recon['complete']:
        status, reason = '待核', 'no_complete_matching'
    elif payable == 0 and not recon['batches']:
        status, reason = '可付', 'empty_bill'
    elif payable == 0:
        status, reason = '待核', 'attribution_unproven'
    else:
        status, reason = '可付', 'strict_attribution' if policy == 'strict' else 'guaranteed_lower_bound'
    body = {'decision_id': decision_id, 'bill_source': source, 'policy': policy,
            'complete_matching': recon['complete'], 'payable_cents': payable,
            'previously_paid_cents': paid_before, 'payment_cents': pay_now,
            'clawback_cents': claw_now,
            'bounds': {'strict': recon['payable']['strict'], 'lower_bound': recon['payable']['lower_bound'],
                       'upper_bound': recon['payable']['upper_bound']},
            'definite_lines': recon['payable']['definite_lines'],
            'guaranteed_lines': recon['payable']['guaranteed_lines']}
    save(db, 'payment_decision', decision_id, trigger_source, version, body, status, reason)
    decide(db, '付款核对', decision_id, status, reason, trigger_source)
    voucher_id = None
    if pay_now:
        voucher_id = f'voucher:{decision_id}'
        voucher_body = {'voucher_id': voucher_id, 'decision_id': decision_id,
                        'bill_source': source, 'amount_cents': pay_now, 'policy': policy}
        save(db, 'payment_voucher', voucher_id, trigger_source, version,
             voucher_body, '已支付', 'payment_voucher')
        decide(db, '付款核对', voucher_id, '已支付', 'payment_voucher', trigger_source)
    if claw_now:
        claw_id = f'clawback:{decision_id}'
        claw_body = {'clawback_id': claw_id, 'decision_id': decision_id,
                     'bill_source': source, 'amount_cents': claw_now,
                     'previously_paid_cents': paid_before, 'payable_cents': payable}
        save(db, 'clawback', claw_id, trigger_source, version, claw_body,
             '待追收', 'clawback_balance')
        decide(db, '付款核对', claw_id, '待追收', 'clawback_balance', trigger_source)
    return {'decision_id': decision_id, 'status': status, 'reason': reason, 'policy': policy,
            'payable_cents': payable, 'previously_paid_cents': paid_before,
            'payment_cents': pay_now, 'clawback_cents': claw_now,
            'voucher_id': voucher_id}


def pay(path, file, policy):
    source, payload = capture(path, 'carrier_bill_export', file)
    db = connect(path)
    try:
        with db:
            if not db.execute('SELECT 1 FROM bill_reconciliation WHERE source_id=?', (source,)).fetchone():
                raise ValueError('bill_not_imported')
            reconcile(db, source, payload['version'])
            return _record_payment(db, source, policy, source, payload['version'])
    finally:
        db.close()


def on_handoff_disputed(db, handoff_id, trigger_source, version):
    """Re-reconcile every bill that could charge this handoff; reopen payment positions."""
    rows = db.execute(
        "SELECT DISTINCT source_id FROM bill_candidate WHERE EXISTS "
        "(SELECT 1 FROM json_each(bill_candidate.body, '$.candidates') WHERE value=?)",
        (handoff_id,)).fetchall()
    sources = [r['source_id'] for r in rows]
    outcomes = []
    for source in sources:
        bill_version = db.execute('SELECT version FROM export_file WHERE id=?', (source,)).fetchone()['version']
        reconcile(db, source, bill_version)
        last = db.execute(
            "SELECT body FROM payment_decision WHERE json_extract(body,'$.bill_source')=? "
            "ORDER BY rowid DESC LIMIT 1", (source,)).fetchone()
        if last is not None:
            policy = _load_json(last)['policy']
            outcomes.append(_record_payment(db, source, policy, trigger_source, version))
    return outcomes
