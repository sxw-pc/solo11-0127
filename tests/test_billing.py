"""Carrier offline bill ingestion and payment reconciliation tests."""
import hashlib
import json
from pathlib import Path

from labeldispatch import billing, disputes, operations, report
from labeldispatch.store import connect

FIXTURES = Path(__file__).parent / 'fixtures'


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    return p


def make_handoffs(root, db, specs, template='T', weight=1.0):
    """Create valid print copies and carrier handoffs from (id, parcel, batch) specs."""
    copies = [{'id': f'C{i}', 'parcel': p, 'paper_no': f'N{i}', 'template': template,
               'required_template': template, 'printed': True}
              for i, (_, p, _) in enumerate(specs, 1)]
    operations.first(db, write(root, 'spool.json', {'version': 'v1', 'rows': copies}))
    rows = []
    for (hid, parcel, batch), i in zip(specs, range(1, len(specs) + 1)):
        rows.append({'id': hid, 'parcel': parcel, 'copy': f'C{i}',
                     'actual_weight': weight, 'declared_weight': weight, 'tolerance': 0.1,
                     'courier': 'K', 'ack_courier': 'K', 'ack_copy': f'C{i}',
                     'batch': batch, 'handoff_date': '2026-01-31',
                     'service_tier': 'std', 'billing_window': 'W1'})
    operations.third(db, write(root, 'handoff.json', {'version': 'v1', 'rows': rows}))
    return rows


def settle_batch(root, db, batch_id, handoff_ids):
    operations.settle(db, write(root, f'settlement-{batch_id}.json',
                                {'version': 'v1', 'rows': [{'id': batch_id, 'handoffs': handoff_ids}]}))


def reopen_dispute(root, db, handoff_id, claim_id='D1'):
    row = {'version': 'v1', 'rows': [{'id': claim_id, 'handoff': handoff_id,
                                      'observed_copy': 'foreign-copy', 'operator': 'K'}]}
    disputes.claim(db, write(root, f'claim-{claim_id}.json', row))


def bill_rows(lines):
    return {'version': 'v1', 'rows': lines}


def recon(db, source):
    row = connect(db).execute('SELECT body FROM bill_reconciliation WHERE source_id=?', (source,)).fetchone()
    return json.loads(row['body'])


def source_id(db, kind, name):
    digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
    return connect(db).execute('SELECT id FROM export_file WHERE kind=? AND hash=?', (kind, digest)).fetchone()['id']


def _build_ambiguous_bill(root, db, disputed=True):
    """The worked example: settled batch B1 with H1 normal and H2 reopened by a
    carrier dispute, plus an anonymous two-line bill where either line may
    belong to either handoff under the real shared fields."""
    make_handoffs(root, db, [('H1', 'P1', 'B1'), ('H2', 'P2', 'B1')])
    settle_batch(root, db, 'B1', ['H1', 'H2'])
    if disputed:
        reopen_dispute(root, db, 'H2', claim_id='D-amb')
    bill = bill_rows([
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1000},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1400}])
    file = write(root, 'bill.json', bill)
    billing.import_bill(db, file)
    return file, bill


def test_ambiguous_two_lines_have_bounds_but_no_proven_attribution(tmp_path):
    db = tmp_path / 'ledger.db'
    file, bill = _build_ambiguous_bill(root=tmp_path, db=db)
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    assert body['complete'] is True
    # H2 is disputed. In matching A H1 takes the 1000 line (pay 1000); in
    # matching B H1 takes the 1400 line (pay 1400). The provable range is
    # [1000, 1400]; the 2400 total can never be claimed.
    assert body['payable']['strict'] == 0
    assert body['payable']['lower_bound'] == 1000
    assert body['payable']['upper_bound'] == 1400
    assert body['batches']['B1']['matching_count'] == 2
    # Neither line is proven to belong to either handoff.
    assert body['payable']['definite_lines'] == []
    candidates = [json.loads(r['body']) for r in
                  connect(db).execute('SELECT body FROM bill_candidate ORDER BY id')]
    assert sorted(candidates[0]['candidates']) == ['H1', 'H2']
    statuses = [r['status'] for r in connect(db).execute('SELECT status FROM bill_candidate ORDER BY id')]
    assert statuses == ['有效', '有效']
    # Strict policy: pay nothing until line ownership is proven.
    strict = billing.pay(db, file, 'strict')
    assert strict['status'] == '待核' and strict['payable_cents'] == 0
    assert strict['payment_cents'] == 0 and strict['voucher_id'] is None
    assert connect(db).execute('SELECT count(*) FROM payment_voucher').fetchone()[0] == 0


def test_lower_bound_policy_pays_guaranteed_floor_without_false_attribution(tmp_path):
    db = tmp_path / 'ledger.db'
    file, _ = _build_ambiguous_bill(root=tmp_path, db=db)
    out = billing.pay(db, file, 'lower_bound')
    assert out['status'] == '可付' and out['reason'] == 'guaranteed_lower_bound'
    assert out['payment_cents'] == 1000 and out['payable_cents'] == 1000
    voucher = json.loads(connect(db).execute('SELECT * FROM payment_voucher').fetchone()['body'])
    assert voucher['amount_cents'] == 1000
    # The 1000-cent line must not be claimed as belonging to H1 anywhere.
    body = recon(db, out and connect(db).execute(
        "SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0])
    assert body['payable']['definite_lines'] == []
    # Re-issuing the same instruction must not pay twice.
    again = billing.pay(db, file, 'lower_bound')
    assert again['payment_cents'] == 0 and again['voucher_id'] is None
    assert connect(db).execute('SELECT count(*) FROM payment_voucher').fetchone()[0] == 1
    assert connect(db).execute(
        "SELECT COALESCE(SUM(json_extract(body,'$.amount_cents')),0) FROM payment_voucher").fetchone()[0] == 1000


def test_partial_bill_three_handoffs_two_lines_enumerates_all_injective_matchings(tmp_path):
    db = tmp_path / 'ledger.db'
    make_handoffs(root=tmp_path, db=db,
                  specs=[('H1', 'P1', 'B1'), ('H2', 'P2', 'B1'), ('H3', 'P3', 'B1')])
    settle_batch(tmp_path, db, 'B1', ['H1', 'H2', 'H3'])
    file = write(tmp_path, 'partial.json', bill_rows([
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1000},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1400}]))
    billing.import_bill(db, file)
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    assert body['complete'] is True
    # 3 handoffs choose 2, ordered: 3*2 = 6 injective matchings, none reuses a handoff.
    assert body['batches']['B1']['matching_count'] == 6
    for matching in body['batches']['B1']['matchings']:
        assert len(set(matching.values())) == 2
    assert body['payable']['strict'] == 0
    assert body['payable']['lower_bound'] == 2400
    assert body['payable']['upper_bound'] == 2400


def test_no_complete_matching_keeps_everything_pending(tmp_path):
    db = tmp_path / 'ledger.db'
    make_handoffs(root=tmp_path, db=db, specs=[('H1', 'P1', 'B1')])
    settle_batch(tmp_path, db, 'B1', ['H1'])
    file = write(tmp_path, 'orphan.json', bill_rows([
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'express',
         'billing_window': 'W1', 'amount_cents': 1000},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1400}]))
    result = billing.import_bill(db, file)
    assert [r['reason'] for r in result] == ['bill_line_captured', 'bill_line_captured']
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    assert body['complete'] is False
    assert body['payable'] == {'strict': 0, 'lower_bound': 0, 'upper_bound': 0,
                               'definite_lines': [], 'guaranteed_lines': []}
    for policy in billing.POLICIES:
        out = billing.pay(db, file, policy)
        assert out['status'] == '待核' and out['reason'] == 'no_complete_matching'
        assert out['payment_cents'] == 0
    cand = connect(db).execute("SELECT status,reason FROM bill_candidate ORDER BY id").fetchall()
    assert [r['reason'] for r in cand] == ['no_feasible_handoff', 'field_candidate']
    assert [r['status'] for r in cand] == ['待核', '有效']


def test_equal_price_lines_still_need_attribution_and_keep_handoff_unique(tmp_path):
    db = tmp_path / 'ledger.db'
    make_handoffs(root=tmp_path, db=db, specs=[('H1', 'P1', 'B1'), ('H2', 'P2', 'B1')])
    settle_batch(tmp_path, db, 'B1', ['H1', 'H2'])
    file = write(tmp_path, 'equal.json', bill_rows([
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1200},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1200}]))
    billing.import_bill(db, file)
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    assert body['batches']['B1']['matching_count'] == 2
    # Equal prices must not be mistaken for proven line ownership.
    assert body['payable']['strict'] == 0
    assert body['payable']['lower_bound'] == 2400
    strict = billing.pay(db, file, 'strict')
    assert strict['payment_cents'] == 0


def test_late_reference_narrows_candidates_and_rejudges_without_double_payment(tmp_path):
    db = tmp_path / 'ledger.db'
    bill_file, bill = _build_ambiguous_bill(root=tmp_path, db=db)
    floor = billing.pay(db, bill_file, 'lower_bound')
    assert floor['payment_cents'] == 1000
    raw = bill_file.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    ref = write(tmp_path, 'reference.json', {'version': 'v1', 'source_digest': digest, 'rows': [
        {'batch': 'B1', 'carrier_line_no': 'L1', 'handoff': 'H1'},
        {'batch': 'B1', 'carrier_line_no': 'L2', 'handoff': 'H2'}]})
    # Bill lines currently have no carrier_line_no: reference cannot find them.
    bad = billing.add_reference(db, ref)
    assert all(r['status'] == '待核' for r in bad)
    # Re-issued bill carrying back-filled carrier line numbers, same content identity.
    numbered = write(tmp_path, 'bill-numbered.json', {'version': 'v1', 'rows': [
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1000, 'carrier_line_no': 'L1'},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1400, 'carrier_line_no': 'L2'}]})
    billing.import_bill(db, numbered)
    numbered_digest = hashlib.sha256(numbered.read_bytes()).hexdigest()
    ref2 = write(tmp_path, 'reference2.json', {'version': 'v1', 'source_digest': numbered_digest, 'rows': [
        {'batch': 'B1', 'carrier_line_no': 'L1', 'handoff': 'H1'},
        {'batch': 'B1', 'carrier_line_no': 'L2', 'handoff': 'H2'}]})
    result = billing.add_reference(db, ref2)
    assert [r['status'] for r in result] == ['有效', '有效']
    new_src = connect(db).execute(
        "SELECT id FROM export_file WHERE hash=?", (numbered_digest,)).fetchone()[0]
    body = recon(db, new_src)
    assert body['complete'] is True and body['batches']['B1']['matching_count'] == 1
    # H2 is disputed: its 1400-cent line moves out; only H1's 1000 is strictly payable.
    assert body['payable']['strict'] == 1000
    # Original anonymous bill is retained untouched.
    assert connect(db).execute("SELECT count(*) FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0] == 2
    # Strict re-judgement on the numbered bill: its own voucher chain starts here.
    out = billing.pay(db, numbered, 'strict')
    assert out['previously_paid_cents'] == 0  # different bill source, its own voucher chain
    assert out['payment_cents'] == 1000
    # Paying the same instruction again must never move money twice.
    assert billing.pay(db, numbered, 'strict')['payment_cents'] == 0
    # Original bill source keeps its single 1000 voucher and must not be repaid.
    again = billing.pay(db, bill_file, 'lower_bound')
    assert again['payment_cents'] == 0
    assert connect(db).execute(
        "SELECT count(*) FROM payment_voucher WHERE json_extract(body,'$.bill_source')=?",
        (connect(db).execute("SELECT id FROM export_file WHERE hash=?", (digest,)).fetchone()[0],)
    ).fetchone()[0] == 1


def test_dispute_after_payment_moves_charge_out_and_records_clawback(tmp_path):
    db = tmp_path / 'ledger.db'
    # Both handoffs normal when the bill arrives and the floor is paid.
    bill_file, _ = _build_ambiguous_bill(root=tmp_path, db=db, disputed=False)
    paid = billing.pay(db, bill_file, 'lower_bound')
    assert paid['payment_cents'] == 2400
    # Physical handoff history must remain intact while the batch reopens.
    before = connect(db).execute("SELECT status FROM handoff WHERE id='H2'").fetchone()[0]
    assert before == '已交接'
    reopen_dispute(tmp_path, db, 'H2')
    conn = connect(db)
    assert conn.execute("SELECT status FROM handoff WHERE id='H2'").fetchone()[0] == '争议'
    assert conn.execute("SELECT status FROM handoff_batch WHERE id='B1'").fetchone()[0] == '争议重开'
    # The historical voucher is never rewritten.
    voucher = conn.execute("SELECT * FROM payment_voucher").fetchone()
    assert voucher['status'] == '已支付'
    assert json.loads(voucher['body'])['amount_cents'] == 2400
    # New decision: the floor drops from 2400 to 1000 (H1 always carries at
    # least the 1000 line), so the overpaid 1400 becomes a clawback balance;
    # the historical 2400 voucher itself is never rewritten.
    decision = conn.execute("SELECT * FROM payment_decision ORDER BY rowid DESC LIMIT 1").fetchone()
    dbody = json.loads(decision['body'])
    assert decision['status'] == '追收'
    assert dbody['payable_cents'] == 1000 and dbody['clawback_cents'] == 1400
    claw = conn.execute("SELECT * FROM clawback").fetchone()
    assert claw['status'] == '待追收' and json.loads(claw['body'])['amount_cents'] == 1400
    # Reconciliation keeps the same two feasible matchings but a reduced floor.
    src = conn.execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    assert body['complete'] is True and body['payable']['lower_bound'] == 1000
    assert body['payable']['upper_bound'] == 1400
    assert body['batches']['B1']['matching_count'] == 2
    assert body['references_applied'] == []
    # Once a reference proves H1 owns the 1000 line, the position is unchanged:
    # the 1000 was already paid, the 1400 stays in clawback pending explanation.
    numbered_digest = hashlib.sha256(write(tmp_path, 'numbered.json', {'version': 'v1', 'rows': [
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1000, 'carrier_line_no': 'L1'},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1400, 'carrier_line_no': 'L2'}]}).read_bytes()).hexdigest()
    numbered = tmp_path / 'numbered.json'
    billing.import_bill(db, numbered)
    ref = write(tmp_path, 'ref.json', {'version': 'v1', 'source_digest': numbered_digest, 'rows': [
        {'batch': 'B1', 'carrier_line_no': 'L1', 'handoff': 'H1'},
        {'batch': 'B1', 'carrier_line_no': 'L2', 'handoff': 'H2'}]})
    billing.add_reference(db, ref)
    new_src = conn.execute("SELECT id FROM export_file WHERE hash=?", (numbered_digest,)).fetchone()[0]
    assert recon(db, new_src)['payable']['strict'] == 1000
    out = billing.pay(db, numbered, 'strict')
    assert out['payable_cents'] == 1000 and out['payment_cents'] == 1000
    # The clawback on the original bill source remains exactly 1400.
    assert connect(db).execute(
        "SELECT COALESCE(SUM(json_extract(body,'$.amount_cents')),0) FROM clawback").fetchone()[0] == 1400


def test_duplicate_file_import_is_idempotent(tmp_path):
    db = tmp_path / 'ledger.db'
    file, _ = _build_ambiguous_bill(root=tmp_path, db=db)
    billing.import_bill(db, file)
    billing.pay(db, file, 'lower_bound')
    billing.import_bill(db, file)
    billing.pay(db, file, 'lower_bound')
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM carrier_bill').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM payment_voucher').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM clawback').fetchone()[0] == 0


def test_duplicate_line_number_and_cross_file_variants_are_conflicts(tmp_path):
    db = tmp_path / 'ledger.db'
    make_handoffs(root=tmp_path, db=db, specs=[('H1', 'P1', 'B1'), ('H2', 'P2', 'B1')])
    settle_batch(tmp_path, db, 'B1', ['H1', 'H2'])
    dup = write(tmp_path, 'dup.json', bill_rows([
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1000, 'carrier_line_no': 'L9'},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1200, 'carrier_line_no': 'L9'}]))
    result = billing.import_bill(db, dup)
    assert [r['reason'] for r in result] == ['duplicate_line_no', 'duplicate_line_no']
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    assert recon(db, src)['complete'] is False
    # A second export reuses L9 with a different amount: same identity, variant text.
    variant = write(tmp_path, 'variant.json', bill_rows([
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1099, 'carrier_line_no': 'L9'}]))
    out = billing.import_bill(db, variant)
    assert out[0]['status'] == '冲突' and out[0]['reason'] == 'line_no_variant_conflict'
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM issue WHERE code='line_no_variant_conflict'").fetchone()[0] >= 1
    assert conn.execute("SELECT count(*) FROM carrier_bill WHERE status='冲突'").fetchone()[0] == 3


def test_reference_with_bad_digest_or_mismatch_is_never_treated_as_fact(tmp_path):
    db = tmp_path / 'ledger.db'
    bill_file, _ = _build_ambiguous_bill(root=tmp_path, db=db)
    good_digest = hashlib.sha256(bill_file.read_bytes()).hexdigest()
    bad_digest = write(tmp_path, 'ref-bad.json', {'version': 'v1', 'source_digest': 'deadbeef', 'rows': [
        {'batch': 'B1', 'carrier_line_no': 'L1', 'handoff': 'H1'}]})
    out = billing.add_reference(db, bad_digest)
    assert out[0]['status'] == '待核' and out[0]['reason'] == 'source_digest_mismatch'
    # Digest of an unrelated export kind must also be rejected.
    handoff_digest = hashlib.sha256((tmp_path / 'handoff.json').read_bytes()).hexdigest()
    wrong_kind = write(tmp_path, 'ref-kind.json', {'version': 'v1', 'source_digest': handoff_digest, 'rows': [
        {'batch': 'B1', 'carrier_line_no': 'L1', 'handoff': 'H1'}]})
    out = billing.add_reference(db, wrong_kind)
    assert out[0]['reason'] == 'source_digest_mismatch'
    # Replaying the rejected file changes nothing.
    assert billing.add_reference(db, bad_digest) == billing.add_reference(db, bad_digest)
    src = connect(db).execute("SELECT id FROM export_file WHERE hash=?", (good_digest,)).fetchone()[0]
    body = recon(db, src)
    assert body['references_applied'] == []
    assert body['batches']['B1']['matching_count'] == 2


def test_synthetic_fixture_export_runs_end_to_end(tmp_path):
    db = tmp_path / 'ledger.db'
    root = tmp_path
    # Reconstruct the physical chain the fixture bill refers to.
    make_handoffs(root, db, [('H1', 'P1', 'B9'), ('H2', 'P2', 'B9')])
    settle_batch(root, db, 'B9', ['H1', 'H2'])
    # Stage 1: anonymous carrier bill — both lines may belong to either handoff.
    anon = FIXTURES / 'carrier-bill-B9.json'
    billing.import_bill(db, anon)
    anon_src = source_id(db, 'carrier_bill_export', 'carrier-bill-B9.json')
    body = recon(db, anon_src)
    assert body['complete'] is True
    assert body['batches']['B9']['matching_count'] == 2
    assert body['payable']['strict'] == 0
    assert body['payable']['lower_bound'] == 2400
    assert body['payable']['upper_bound'] == 2400
    # Stage 2: carrier re-exports the same bill with line numbers, then sends a
    # digest-backed line-number cross-reference that narrows the candidates.
    numbered = FIXTURES / 'carrier-bill-B9-numbered.json'
    billing.import_bill(db, numbered)
    numbered_src = source_id(db, 'carrier_bill_export', 'carrier-bill-B9-numbered.json')
    # Even with line numbers, without the reference both handoffs still match.
    assert recon(db, numbered_src)['batches']['B9']['matching_count'] == 2
    result = billing.add_reference(db, FIXTURES / 'line-reference-B9.json')
    assert [r['status'] for r in result] == ['有效', '有效']
    body = recon(db, numbered_src)
    assert body['batches']['B9']['matching_count'] == 1
    assert body['payable']['strict'] == 2400
    assert body['references_applied'] == [connect(db).execute(
        "SELECT id FROM export_file WHERE kind='carrier_line_reference'").fetchone()[0]]
    out = billing.pay(db, numbered, 'strict')
    assert out['payment_cents'] == 2400
    # The original anonymous bill and its decisions remain untouched.
    assert connect(db).execute("SELECT count(*) FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0] == 2
    report_path = report.build(db, tmp_path / 'report.json')
    data = json.loads(report_path.read_text(encoding='utf-8'))
    paid_sources = {b['bill_source']: b['paid_cents'] for b in data['billing']}
    assert paid_sources[numbered_src] == 2400 and paid_sources[anon_src] == 0
    assert 'carrier_bill' in data['entities'] and 'payment_voucher' in data['entities']
    assert all(s['verified'] for s in data['sources'])


def test_report_and_sqlite_expose_candidates_vouchers_and_clawback(tmp_path):
    db = tmp_path / 'ledger.db'
    # Floor of 1000 paid while H2 is already disputed (the worked example).
    bill_file, _ = _build_ambiguous_bill(root=tmp_path, db=db, disputed=True)
    billing.pay(db, bill_file, 'lower_bound')
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    entry = data['billing'][0]
    assert entry['paid_cents'] == 1000 and entry['clawback_cents'] == 0
    assert entry['payable']['lower_bound'] == 1000
    conn = connect(db)
    candidates = [json.loads(r['body']) for r in conn.execute('SELECT body FROM bill_candidate')]
    assert all(set(c['candidates']) == {'H1', 'H2'} for c in candidates)
    decisions = [r['reason'] for r in conn.execute('SELECT reason FROM payment_decision')]
    assert 'guaranteed_lower_bound' in decisions
    assert conn.execute("SELECT count(*) FROM decision_log WHERE subsystem='付款核对'").fetchone()[0] >= 2
    # Now drive a clawback through a fresh dispute on a fully paid bill.
    db2 = tmp_path / 'ledger2.db'
    bill2, _ = _build_ambiguous_bill(root=tmp_path, db=db2, disputed=False)
    billing.pay(db2, bill2, 'lower_bound')
    reopen_dispute(tmp_path, db2, 'H2', claim_id='D9')
    data2 = json.loads(report.build(db2, tmp_path / 'report2.json').read_text(encoding='utf-8'))
    entry2 = data2['billing'][0]
    assert entry2['clawback_cents'] == 1400 and entry2['paid_cents'] == 2400
    assert entry2['payable']['lower_bound'] == 1000
    conn2 = connect(db2)
    decisions2 = [r['reason'] for r in conn2.execute('SELECT reason FROM payment_decision')]
    assert 'disputed_charge_moved_out' in decisions2
    assert conn2.execute("SELECT status FROM clawback").fetchone()[0] == '待追收'


def test_multi_batch_bill_sums_bounds_per_batch(tmp_path):
    db = tmp_path / 'ledger.db'
    make_handoffs(root=tmp_path, db=db, specs=[('H1', 'P1', 'B1'), ('H2', 'P2', 'B2')])
    settle_batch(tmp_path, db, 'B1', ['H1'])
    settle_batch(tmp_path, db, 'B2', ['H2'])
    file = write(tmp_path, 'multi.json', bill_rows([
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1000},
        {'batch': 'B2', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1400}]))
    billing.import_bill(db, file)
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    assert body['complete'] is True
    assert {k: v['matching_count'] for k, v in body['batches'].items()} == {'B1': 1, 'B2': 1}
    # Each batch has a single feasible matching, so all three figures agree.
    assert body['payable']['strict'] == 2400
    assert body['payable']['lower_bound'] == 2400
    assert body['payable']['upper_bound'] == 2400
    assert billing.pay(db, file, 'strict')['payment_cents'] == 2400


def test_multi_batch_bounds_add_across_disputed_and_clean_batches(tmp_path):
    db = tmp_path / 'ledger.db'
    make_handoffs(root=tmp_path, db=db,
                  specs=[('H1', 'P1', 'B1'), ('H2', 'P2', 'B1'), ('H3', 'P3', 'B2')])
    settle_batch(tmp_path, db, 'B1', ['H1', 'H2'])
    settle_batch(tmp_path, db, 'B2', ['H3'])
    reopen_dispute(tmp_path, db, 'H2', claim_id='D1')
    file = write(tmp_path, 'multi2.json', bill_rows([
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1000},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1400},
        {'batch': 'B2', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 500}]))
    billing.import_bill(db, file)
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    # B1 alone contributes [1000, 1400]; clean B2 always contributes its 500.
    assert body['payable']['lower_bound'] == 1500
    assert body['payable']['upper_bound'] == 1900
    assert body['payable']['strict'] == 500
    out = billing.pay(db, file, 'lower_bound')
    assert out['payment_cents'] == 1500


def test_reference_for_one_bill_never_leaks_into_another_bill(tmp_path):
    db = tmp_path / 'ledger.db'
    make_handoffs(root=tmp_path, db=db, specs=[('H1', 'P1', 'B1'), ('H2', 'P2', 'B1')])
    settle_batch(tmp_path, db, 'B1', ['H1', 'H2'])
    rows = [
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1000, 'carrier_line_no': 'L1'},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1400, 'carrier_line_no': 'L2'}]
    first = write(tmp_path, 'bill-a.json', {'version': 'v1', 'rows': rows})
    billing.import_bill(db, first)
    # Identical line numbers and content in a second, separately exported bill
    # file (same identity, different export hash).
    second = write(tmp_path, 'bill-b.json', {'version': 'v2', 'rows': [dict(r) for r in rows]})
    billing.import_bill(db, second)
    digest_a = hashlib.sha256(first.read_bytes()).hexdigest()
    ref = write(tmp_path, 'ref-a.json', {'version': 'v1', 'source_digest': digest_a, 'rows': [
        {'batch': 'B1', 'carrier_line_no': 'L1', 'handoff': 'H1'},
        {'batch': 'B1', 'carrier_line_no': 'L2', 'handoff': 'H2'}]})
    assert [r['status'] for r in billing.add_reference(db, ref)] == ['有效', '有效']
    src_a = connect(db).execute("SELECT id FROM export_file WHERE hash=?", (digest_a,)).fetchone()[0]
    src_b = connect(db).execute(
        "SELECT id FROM export_file WHERE hash=?", (hashlib.sha256(second.read_bytes()).hexdigest(),)).fetchone()[0]
    # Only bill A is narrowed; bill B keeps the full two-candidate ambiguity.
    assert recon(db, src_a)['batches']['B1']['matching_count'] == 1
    body_b = recon(db, src_b)
    assert body_b['batches']['B1']['matching_count'] == 2
    assert body_b['references_applied'] == []


def test_one_handoff_cannot_carry_two_positive_charges(tmp_path):
    db = tmp_path / 'ledger.db'
    make_handoffs(root=tmp_path, db=db, specs=[('H1', 'P1', 'B1')])
    settle_batch(tmp_path, db, 'B1', ['H1'])
    file = write(tmp_path, 'two-charges.json', bill_rows([
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1000},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 1400}]))
    billing.import_bill(db, file)
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    # Both lines only fit H1; an injective matching covering both is impossible.
    assert body['complete'] is False
    assert body['batches']['B1']['matching_count'] == 0
    assert billing.pay(db, file, 'lower_bound')['payment_cents'] == 0


def test_dispute_without_prior_payment_creates_no_voucher_or_clawback(tmp_path):
    db = tmp_path / 'ledger.db'
    file, _ = _build_ambiguous_bill(root=tmp_path, db=db, disputed=False)
    # Bill imported and reconciled, but finance never paid.
    reopen_dispute(tmp_path, db, 'H2')
    conn = connect(db)
    assert conn.execute('SELECT count(*) FROM payment_decision').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM payment_voucher').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM clawback').fetchone()[0] == 0
    src = conn.execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    assert body['complete'] is True
    assert body['payable']['strict'] == 0
    assert body['payable']['lower_bound'] == 1000
    assert body['payable']['upper_bound'] == 1400
    # Paying the floor afterwards still works and must not invent history.
    out = billing.pay(db, file, 'lower_bound')
    assert out['previously_paid_cents'] == 0 and out['payment_cents'] == 1000


def test_empty_bill_is_complete_with_zero_payable(tmp_path):
    db = tmp_path / 'ledger.db'
    file = write(tmp_path, 'empty.json', {'version': 'v1', 'rows': []})
    assert billing.import_bill(db, file) == []
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    body = recon(db, src)
    assert body['complete'] is True and body['batches'] == {}
    out = billing.pay(db, file, 'strict')
    assert out['status'] == '可付' and out['reason'] == 'empty_bill'
    assert out['payment_cents'] == 0 and out['voucher_id'] is None
    assert connect(db).execute('SELECT count(*) FROM payment_voucher').fetchone()[0] == 0


def test_missing_fields_and_non_integer_amounts_stay_pending(tmp_path):
    db = tmp_path / 'ledger.db'
    make_handoffs(root=tmp_path, db=db, specs=[('H1', 'P1', 'B1')])
    settle_batch(tmp_path, db, 'B1', ['H1'])
    bad = write(tmp_path, 'bad-bill.json', bill_rows([
        {'batch': 'B1', 'service_tier': 'std', 'billing_window': 'W1', 'amount_cents': 1000},
        {'batch': 'B1', 'handoff_date': '2026-01-31', 'service_tier': 'std',
         'billing_window': 'W1', 'amount_cents': 0}]))
    result = billing.import_bill(db, bad)
    assert [r['reason'] for r in result] == ['bill_field_missing', 'amount_invalid']
    src = connect(db).execute("SELECT id FROM export_file WHERE kind='carrier_bill_export'").fetchone()[0]
    assert recon(db, src)['complete'] is False
