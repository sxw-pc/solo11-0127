"""Tests for authenticated physical printer receipts and the evidence chain."""
import json
import tempfile
from pathlib import Path

from labeldispatch import operations, report, resources
from labeldispatch.ingest import file_digest, receipt_signature
from labeldispatch.store import connect

# Fictional test keys only; real deployments supply the key file at runtime.
KEYS = {'printers': {'P1': {'active': 'k2', 'keys': {'k1': 'fictional-old', 'k2': 'fictional-new'}}}}


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj), encoding='utf-8')
    return p


def keys_file(root, keys=KEYS):
    return write(root, 'keys.json', keys)


def plan_one(db, root, jobs=None, paper=5):
    jobs = jobs or [{'copy_id': 'C1', 'template': 'T1', 'paper': 'A6'}]
    return resources.plan(db, write(root, 'plan.json', {
        'version': 'v1',
        'printers': [{'id': 'P1', 'templates': ['T1'], 'paper': {'A6': paper}, 'capacity': 5, 'online': True}],
        'jobs': jobs}))


def spool(root, rows, name='spool.json'):
    return write(root, name, {'version': 'v1', 'rows': rows})


def receipt(copy_id='C1', paper='N1', template='T1', seq=1, key_id='k2', secret='fictional-new',
            digest=None, printer='P1', result='printed', receipt_id=None, **overrides):
    message = {'receipt_id': receipt_id or f'R-{copy_id}-{seq}',
               'printer': printer, 'copy_id': copy_id, 'paper_no': paper, 'template': template,
               'seq': seq, 'result': result, 'export_digest': digest, 'key_id': key_id}
    message.update(overrides)
    message['signature'] = receipt_signature(message, secret)
    return message


def receipt_file(root, receipts, name='receipts.json'):
    return write(root, name, {'version': 'v1', 'receipts': receipts})


def copy_row(db, copy_id):
    row = connect(db).execute('SELECT * FROM print_copy WHERE id=?', (copy_id,)).fetchone()
    return dict(row) if row else None


def reservation_status(db, copy_id):
    row = connect(db).execute('SELECT status FROM printer_reservation WHERE id=?', (copy_id,)).fetchone()
    return row[0] if row else None


def consumed(db):
    return connect(db).execute('SELECT count(*) FROM paper_consumption').fetchone()[0]


def one_copy_setup(root):
    db = root / 'ledger.db'
    plan_one(db, root)
    file = spool(root, [{'id': 'C1', 'parcel': 'P1', 'paper_no': 'N1', 'template': 'T1',
                         'required_template': 'T1', 'printed': True}])
    return db, file, file_digest(file)


def report_data(db, root):
    return json.loads(report.build(db, root / 'report.json').read_text(encoding='utf-8'))


def test_authenticated_queue_claim_alone_never_becomes_valid(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    result = operations.first(db, file, keys_file(tmp_path))
    assert result == [{'subject': 'C1', 'status': '待核', 'reason': 'receipt_missing'}]
    assert copy_row(db, 'C1')['status'] == '待核'
    assert reservation_status(db, 'C1') == '已预留'
    assert consumed(db) == 0
    data = report_data(db, tmp_path)
    assert data['evidence']['valid_copies'] == []
    assert data['evidence']['pending_copies'][0]['reason'] == 'receipt_missing'
    assert data['evidence']['paper_stock'][0]['consumed'] == 0


def test_signed_receipt_makes_copy_valid_and_consumes_paper(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    operations.first(db, file, keys_file(tmp_path))
    result = operations.receipts(db, receipt_file(tmp_path, [receipt(digest=digest)]), keys_file(tmp_path))
    assert result[0]['status'] == '已认证' and result[0]['reason'] == 'device_signed'
    row = copy_row(db, 'C1')
    assert (row['status'], row['reason']) == ('有效', 'device_receipt_verified')
    assert reservation_status(db, 'C1') == '已消耗'
    assert consumed(db) == 1
    data = report_data(db, tmp_path)
    valid = data['evidence']['valid_copies']
    assert valid[0]['evidence_level'] == 'device'
    assert valid[0]['queue_export_hash'] == digest
    assert valid[0]['printer'] == 'P1'
    stock = data['evidence']['paper_stock'][0]
    assert (stock['snapshot_available'], stock['consumed'], stock['available']) == (5, 1, 4)


def test_bad_signature_is_rejected_and_consumes_nothing(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    operations.first(db, file, keys_file(tmp_path))
    forged = receipt(digest=digest)
    forged['paper_no'] = 'N1'  # signature was made before alteration below
    forged['signature'] = receipt_signature(dict(forged, paper_no='N9'), 'fictional-new')
    result = operations.receipts(db, receipt_file(tmp_path, [forged]), keys_file(tmp_path))
    assert result[0]['status'] == '拒绝' and result[0]['reason'] == 'bad_signature'
    assert copy_row(db, 'C1')['status'] == '待核'
    assert reservation_status(db, 'C1') == '已预留'
    assert consumed(db) == 0


def test_paper_number_mismatch_blocks_success(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    operations.first(db, file, keys_file(tmp_path))
    result = operations.receipts(db, receipt_file(tmp_path, [receipt(paper='OTHER', digest=digest)]),
                                 keys_file(tmp_path))
    assert result[0]['status'] == '已认证'  # signature is genuine ...
    row = copy_row(db, 'C1')
    assert (row['status'], row['reason']) == ('待核', 'receipt_paper_mismatch')  # ... but does not bind
    assert reservation_status(db, 'C1') == '已预留'
    assert consumed(db) == 0


def test_device_sequence_regression_is_rejected(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    operations.first(db, file, keys_file(tmp_path))
    assert operations.receipts(db, receipt_file(tmp_path, [receipt(seq=5, digest=digest, receipt_id='R5')],
                                                'r5.json'), keys_file(tmp_path))[0]['status'] == '已认证'
    stale = operations.receipts(db, receipt_file(tmp_path, [receipt(seq=4, digest=digest, receipt_id='R4')],
                                                'r4.json'), keys_file(tmp_path))[0]
    assert (stale['status'], stale['reason']) == ('拒绝', 'sequence_regression')
    assert consumed(db) == 1


def test_same_sequence_different_content_is_rejected(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    operations.first(db, file, keys_file(tmp_path))
    operations.receipts(db, receipt_file(tmp_path, [receipt(seq=3, digest=digest, receipt_id='Ra')], 'ra.json'),
                        keys_file(tmp_path))
    divergent = receipt(seq=3, digest=digest, receipt_id='Rb', paper_no='N1', result='printed',
                        printer='P1')
    divergent['receipt_id'] = 'Rb'
    divergent['signature'] = receipt_signature(divergent, 'fictional-new')
    result = operations.receipts(db, receipt_file(tmp_path, [divergent], 'rb.json'), keys_file(tmp_path))[0]
    assert (result['status'], result['reason']) == ('拒绝', 'sequence_conflict')
    assert consumed(db) == 1


def test_paper_reused_by_another_copy_cannot_be_claimed(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_one(db, tmp_path, jobs=[{'copy_id': 'C1', 'template': 'T1', 'paper': 'A6'},
                                {'copy_id': 'C2', 'template': 'T1', 'paper': 'A6'}], paper=5)
    file = spool(tmp_path, [
        {'id': 'C1', 'parcel': 'P1', 'paper_no': 'N1', 'template': 'T1', 'required_template': 'T1', 'printed': True},
        {'id': 'C2', 'parcel': 'P2', 'paper_no': 'N1', 'template': 'T1', 'required_template': 'T1', 'printed': True}])
    digest = file_digest(file)
    result = operations.first(db, file, keys_file(tmp_path))
    assert result[0]['reason'] == 'paper_reused'
    operations.receipts(db, receipt_file(tmp_path, [receipt('C1', digest=digest, receipt_id='R1')]), keys_file(tmp_path))
    operations.receipts(db, receipt_file(tmp_path, [receipt('C2', parcel='P2', digest=digest, receipt_id='R2')]
                                                , 'r2.json'), keys_file(tmp_path))
    assert copy_row(db, 'C1')['status'] == '冲突'
    assert copy_row(db, 'C2')['status'] == '冲突'
    assert consumed(db) == 0
    assert reservation_status(db, 'C1') == '已预留'


def test_key_rotation_keeps_old_receipts_verifiable_and_no_double_consumption(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    before = keys_file(tmp_path, {'printers': {'P1': {'active': 'k1', 'keys': {'k1': 'fictional-old'}}}})
    operations.first(db, file, before)
    old = receipt(seq=1, key_id='k1', secret='fictional-old', digest=digest, receipt_id='OLD1')
    assert operations.receipts(db, receipt_file(tmp_path, [old]), before)[0]['status'] == '已认证'
    # Rotate: k2 active, k1 retained for verifying historical receipts.
    rotated = keys_file(tmp_path)
    operations.receipts(db, receipt_file(tmp_path, [receipt(seq=2, key_id='k2', secret='fictional-new',
                                                           digest=digest, receipt_id='NEW1')], 'new.json'),
                        rotated)
    assert consumed(db) == 1
    # Replaying the old signed message after rotation must not add stock again.
    assert operations.receipts(db, receipt_file(tmp_path, [old], 'replay.json'), rotated)[0]['status'] == '已认证'
    assert consumed(db) == 1
    assert reservation_status(db, 'C1') == '已消耗'
    # A brand-new receipt under the retired key is refused.
    fresh_old_key = receipt(seq=9, key_id='k1', secret='fictional-old', digest=digest, receipt_id='OLD2')
    result = operations.receipts(db, receipt_file(tmp_path, [fresh_old_key], 'old2.json'), rotated)[0]
    assert (result['status'], result['reason']) == ('拒绝', 'key_version_not_active')


def test_rotated_key_file_without_old_version_breaks_old_receipt(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    before = keys_file(tmp_path, {'printers': {'P1': {'active': 'k1', 'keys': {'k1': 'fictional-old'}}}})
    operations.first(db, file, before)
    old = receipt(seq=1, key_id='k1', secret='fictional-old', digest=digest, receipt_id='OLD1')
    operations.receipts(db, receipt_file(tmp_path, [old]), before)
    stripped = {'printers': {'P1': {'active': 'k2', 'keys': {'k2': 'fictional-new'}}}}
    replay = operations.receipts(db, receipt_file(tmp_path, [old], 'replay.json'), keys_file(tmp_path, stripped))[0]
    assert (replay['status'], replay['reason']) == ('拒绝', 'unknown_key_version')
    # The retained ledger still shows the original acceptance and consumption.
    assert consumed(db) == 1
    stored = connect(db).execute('SELECT status FROM printer_receipt WHERE id=?', (replay['receipt_id'],)).fetchone()
    assert stored[0] == '已认证'


def test_dual_witness_offline_contract_requires_two_operators(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    keys = keys_file(tmp_path)
    # Device offline: no receipt at all.
    assert operations.first(db, file, keys)[0]['reason'] == 'receipt_missing'
    one = operations.witnesses(db, write(tmp_path, 'w1.json', {'version': 'v1', 'witnesses': [
        {'id': 'W1', 'copy_id': 'C1', 'paper_no': 'N1', 'operator': 'alice', 'scan_ref': 'scan:1'}]}))
    assert one[0]['status'] == '已记录'
    assert copy_row(db, 'C1')['status'] == '待核'
    assert copy_row(db, 'C1')['reason'] == 'witness_incomplete'
    assert reservation_status(db, 'C1') == '已预留'
    assert consumed(db) == 0
    two = operations.witnesses(db, write(tmp_path, 'w2.json', {'version': 'v1', 'witnesses': [
        {'id': 'W2', 'copy_id': 'C1', 'paper_no': 'N1', 'operator': 'bob', 'scan_ref': 'scan:2'}]}))
    assert copy_row(db, 'C1')['status'] == '有效'
    assert copy_row(db, 'C1')['reason'] == 'dual_witness_verified'
    assert reservation_status(db, 'C1') == '已消耗'
    assert consumed(db) == 1
    data = report_data(db, tmp_path)
    assert data['evidence']['valid_copies'][0]['evidence_level'] == 'witness'
    assert set(data['evidence']['valid_copies'][0]['witness_operators']) == {'alice', 'bob'}


def test_dual_witness_paper_mismatch_and_same_operator_conflict(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    operations.first(db, file, keys_file(tmp_path))
    operations.witnesses(db, write(tmp_path, 'w1.json', {'version': 'v1', 'witnesses': [
        {'id': 'W1', 'copy_id': 'C1', 'paper_no': 'N1', 'operator': 'alice', 'scan_ref': 'scan:1'}]}))
    # Same operator twice is still one witness.
    operations.witnesses(db, write(tmp_path, 'w2.json', {'version': 'v1', 'witnesses': [
        {'id': 'W2', 'copy_id': 'C1', 'paper_no': 'N1', 'operator': 'alice', 'scan_ref': 'scan:2'}]}))
    assert copy_row(db, 'C1')['status'] == '待核'
    assert reservation_status(db, 'C1') == '已预留'
    # A second operator who saw a different paper number is a conflict, not a pass.
    operations.witnesses(db, write(tmp_path, 'w3.json', {'version': 'v1', 'witnesses': [
        {'id': 'W3', 'copy_id': 'C1', 'paper_no': 'N9', 'operator': 'bob', 'scan_ref': 'scan:3'}]}))
    assert copy_row(db, 'C1')['status'] == '冲突'
    assert consumed(db) == 0


def test_device_contract_and_witness_contract_diverge_for_same_claim(tmp_path):
    """Same printed=true, one pending under device-only, valid under dual scan."""
    device_db = tmp_path / 'device.db'
    witness_db = tmp_path / 'witness.db'
    for db, name in ((device_db, 'd'), (witness_db, 'w')):
        plan_one(db, tmp_path if name == 'd' else tmp_path, jobs=[{'copy_id': 'C1', 'template': 'T1', 'paper': 'A6'}])
    file = spool(tmp_path, [{'id': 'C1', 'parcel': 'P1', 'paper_no': 'N1', 'template': 'T1',
                             'required_template': 'T1', 'printed': True}])
    operations.first(device_db, file, keys_file(tmp_path))
    operations.first(witness_db, file, keys_file(tmp_path))
    # Device-only path: receipt never arrives -> still pending and not handoff-able.
    assert copy_row(device_db, 'C1')['status'] == '待核'
    handoff = write(tmp_path, 'handoff.json', {'version': 'v1', 'rows': [
        {'id': 'H1', 'parcel': 'P1', 'copy': 'C1', 'actual_weight': 1, 'declared_weight': 1,
         'tolerance': 0.1, 'courier': 'K', 'ack_courier': 'K', 'ack_copy': 'C1'}]})
    assert operations.third(device_db, handoff)[0]['status'] == '拒交'
    # Witness path: two independent scans -> valid and handoff-able.
    for i, operator in enumerate(('alice', 'bob'), 1):
        operations.witnesses(witness_db, write(tmp_path, f'w{i}.json', {'version': 'v1', 'witnesses': [
            {'id': f'W{i}', 'copy_id': 'C1', 'paper_no': 'N1', 'operator': operator, 'scan_ref': f'scan:{i}'}]}))
    assert copy_row(witness_db, 'C1')['status'] == '有效'
    assert operations.third(witness_db, handoff)[0]['status'] == '已交接'


def test_missing_receipt_then_late_receipt_is_applied(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    operations.first(db, file, keys_file(tmp_path))
    assert copy_row(db, 'C1')['reason'] == 'receipt_missing'
    # Faulty device: queue and reservation are retained.
    assert reservation_status(db, 'C1') == '已预留'
    assert connect(db).execute('SELECT count(*) FROM export_file WHERE kind=?', ('printer_spool',)).fetchone()[0] == 1
    operations.receipts(db, receipt_file(tmp_path, [receipt(digest=digest)]), keys_file(tmp_path))
    assert copy_row(db, 'C1')['status'] == '有效'
    assert reservation_status(db, 'C1') == '已消耗'
    assert connect(db).execute('SELECT count(*) FROM export_file WHERE kind=?', ('printer_spool',)).fetchone()[0] == 1


def test_same_export_rerun_is_idempotent(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    keys = keys_file(tmp_path)
    operations.first(db, file, keys)
    operations.receipts(db, receipt_file(tmp_path, [receipt(digest=digest)]), keys)
    before = (copy_row(db, 'C1'), reservation_status(db, 'C1'), consumed(db))
    assert operations.first(db, file, keys)[0]['status'] == '有效'
    operations.receipts(db, receipt_file(tmp_path, [receipt(digest=digest)], 'again.json'), keys)
    after = (copy_row(db, 'C1'), reservation_status(db, 'C1'), consumed(db))
    assert before == after
    assert connect(db).execute('SELECT count(*) FROM export_file WHERE kind=?', ('printer_spool',)).fetchone()[0] == 1
    assert connect(db).execute('SELECT count(*) FROM paper_consumption').fetchone()[0] == 1


def test_forged_receipt_after_handoff_cannot_reopen_consumption(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_one(db, tmp_path, jobs=[{'copy_id': 'C1', 'template': 'T1', 'paper': 'A6'}])
    file = spool(tmp_path, [{'id': 'C1', 'parcel': 'P1', 'paper_no': 'N1', 'template': 'T1',
                             'required_template': 'T1', 'printed': True}])
    keys = keys_file(tmp_path)
    operations.first(db, file, keys)
    # Controlled release under the dual-witness contract, then handoff.
    for i, operator in enumerate(('alice', 'bob'), 1):
        operations.witnesses(db, write(tmp_path, f'w{i}.json', {'version': 'v1', 'witnesses': [
            {'id': f'W{i}', 'copy_id': 'C1', 'paper_no': 'N1', 'operator': operator, 'scan_ref': f'scan:{i}'}]}))
    operations.third(db, write(tmp_path, 'handoff.json', {'version': 'v1', 'rows': [
        {'id': 'H1', 'parcel': 'P1', 'copy': 'C1', 'actual_weight': 1, 'declared_weight': 1,
         'tolerance': 0.1, 'courier': 'K', 'ack_courier': 'K', 'ack_copy': 'C1'}]}))
    assert consumed(db) == 1
    # Forged receipt with a different paper number arrives after handoff.
    forged = receipt(paper='N9', digest=file_digest(file), seq=50)
    forged['signature'] = 'deadbeef'
    result = operations.receipts(db, receipt_file(tmp_path, [forged], 'forged.json'), keys)[0]
    assert result['status'] == '拒绝' and result['reason'] == 'bad_signature'
    assert consumed(db) == 1
    assert reservation_status(db, 'C1') == '已消耗'
    assert copy_row(db, 'C1')['status'] == '有效'
    assert connect(db).execute("SELECT count(*) FROM handoff WHERE status='已交接'").fetchone()[0] == 1


def test_authenticated_reprint_waits_for_physical_output(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_one(db, tmp_path, jobs=[{'copy_id': 'C1', 'template': 'T1', 'paper': 'A6'},
                                {'copy_id': 'C2', 'template': 'T1', 'paper': 'A6'}])
    keys = keys_file(tmp_path)
    spool_file = spool(tmp_path, [{'id': 'C1', 'parcel': 'P1', 'paper_no': 'N1', 'template': 'T1',
                                   'required_template': 'T1', 'printed': True}])
    digest = file_digest(spool_file)
    operations.first(db, spool_file, keys)
    operations.receipts(db, receipt_file(tmp_path, [receipt('C1', digest=digest)]), keys)
    request = write(tmp_path, 'reprint.json', {'version': 'v1', 'rows': [
        {'id': 'R1', 'old_copy': 'C1', 'reason': 'damaged', 'approver': 'A',
         'at': '2026-01-01T10:00:00', 'expires': '2026-01-02T00:00:00',
         'new_copy': 'C2', 'new_paper': 'N2'}]})
    assert operations.second(db, request, keys)[0]['status'] == '批准'
    replacement = copy_row(db, 'C2')
    assert (replacement['status'], replacement['reason']) == ('待印', 'awaiting_physical_output')
    assert consumed(db) == 1  # only the original sheet
    # Physical output for the replacement later makes it valid.
    operations.receipts(db, receipt_file(tmp_path, [receipt('C2', paper='N2', seq=2, digest=digest,
                                                           receipt_id='R-C2')], 'c2.json'), keys)
    assert copy_row(db, 'C2')['status'] == '有效'
    assert consumed(db) == 2


def test_legacy_demo_remains_unchanged_and_labelled(tmp_path):
    result = operations.demo(tmp_path)
    assert [x[0]['status'] for x in result] == ['有效', '批准', '已交接', '已结算']
    db = tmp_path / 'ledger.db'
    conn = connect(db)
    assert conn.execute("SELECT status FROM print_copy WHERE id='C1'").fetchone()[0] == '作废'
    assert conn.execute("SELECT status FROM print_copy WHERE id='C2'").fetchone()[0] == '有效'
    assert conn.execute("SELECT count(*) FROM printer_reservation WHERE status='已消耗'").fetchone()[0] == 2
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert len(data['sources']) == 5 and all(s['verified'] for s in data['sources'])
    assert all(c['evidence_level'] == 'legacy' for c in data['evidence']['valid_copies'])
    assert data['evidence']['valid_copies'][0]['authorized'] is True
    assert data['evidence']['valid_copies'][0]['receipts'] == []


def test_reports_never_contain_key_material(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    keys = keys_file(tmp_path)
    operations.first(db, file, keys)
    operations.receipts(db, receipt_file(tmp_path, [receipt(digest=digest)]), keys)
    text = json.dumps(report_data(db, tmp_path), ensure_ascii=False)
    assert 'fictional-new' not in text and 'fictional-old' not in text
    # Signature bytes are redacted to an audit digest inside the stored body.
    entities = report_data(db, tmp_path)['entities']['printer_receipt']
    assert entities[0]['body'].find('"signature"') == -1
    stored = json.loads(entities[0]['body'])
    assert stored['signature_redacted'] is True and len(stored['signature_sha256']) == 64
    assert 'fictional-new' not in stored['signature_sha256']


def test_conflicting_authenticated_receipts_never_prefer_the_convenient_one(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    keys = keys_file(tmp_path)
    operations.first(db, file, keys)
    operations.receipts(db, receipt_file(tmp_path, [receipt(paper='N1', seq=1, receipt_id='R1', digest=digest)],
                                        'r1.json'), keys)
    assert copy_row(db, 'C1')['status'] == '有效'
    # A second signed message for the same copy names another paper number.
    operations.receipts(db, receipt_file(tmp_path, [receipt(paper='N9', seq=2, receipt_id='R2', digest=digest)],
                                        'r2.json'), keys)
    assert (copy_row(db, 'C1')['status'], copy_row(db, 'C1')['reason']) == ('待核', 'receipt_paper_mismatch')
    assert consumed(db) == 1  # the first, verified sheet; no extra consumption


def test_copy_identity_conflict_does_not_overwrite_original(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    keys = keys_file(tmp_path)
    operations.first(db, file, keys)
    operations.receipts(db, receipt_file(tmp_path, [receipt(digest=digest)]), keys)
    # Same copy id, different paper number in another queue export.
    tampered = spool(tmp_path, [{'id': 'C1', 'parcel': 'P1', 'paper_no': 'N9', 'template': 'T1',
                                 'required_template': 'T1', 'printed': True}], 'spool2.json')
    result = operations.first(db, tampered, keys)
    assert result[0]['reason'] == 'copy_identity_conflict'
    row = copy_row(db, 'C1')
    assert json.loads(row['body'])['paper_no'] == 'N1'
    assert consumed(db) == 1


def test_missing_key_file_fails_fast(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        operations.first(db, file, tmp_path / 'nope.json')
    with pytest.raises(ValueError):
        operations.receipts(db, receipt_file(tmp_path, [receipt(digest=digest)]), tmp_path / 'nope.json')
    assert consumed(db) == 0


def test_receipt_printer_must_match_reservation(tmp_path):
    db = tmp_path / 'ledger.db'
    keys = keys_file(tmp_path)
    resources.plan(db, write(tmp_path, 'plan.json', {
        'version': 'v1',
        'printers': [{'id': 'P1', 'templates': ['T1'], 'paper': {'A6': 5}, 'capacity': 5, 'online': True},
                     {'id': 'P2', 'templates': ['T1'], 'paper': {'A6': 5}, 'capacity': 5, 'online': True}],
        'jobs': [{'copy_id': 'C1', 'template': 'T1', 'paper': 'A6'}]}))
    file = spool(tmp_path, [{'id': 'C1', 'parcel': 'P1', 'paper_no': 'N1', 'template': 'T1',
                             'required_template': 'T1', 'printed': True}])
    digest = file_digest(file)
    operations.first(db, file, keys)
    reserved = json.loads(connect(db).execute("SELECT body FROM printer_reservation WHERE id='C1'").fetchone()[0])['printer']
    other = 'P2' if reserved == 'P1' else 'P1'
    keys_two = keys_file(tmp_path, {'printers': {reserved: {'active': 'k2', 'keys': {'k2': 'fictional-new'}},
                                                other: {'active': 'k2', 'keys': {'k2': 'fictional-new'}}}})
    result = operations.receipts(db, receipt_file(tmp_path, [receipt(printer=other, digest=digest)]), keys_two)[0]
    assert result['status'] == '已认证'
    assert (copy_row(db, 'C1')['status'], copy_row(db, 'C1')['reason']) == ('待核', 'receipt_printer_mismatch')
    assert consumed(db) == 0


def test_receipt_for_different_export_is_not_accepted_evidence(tmp_path):
    db, file, digest = one_copy_setup(tmp_path)
    keys = keys_file(tmp_path)
    operations.first(db, file, keys)
    other_export = spool(tmp_path, [{'id': 'C1', 'parcel': 'P1', 'paper_no': 'N1', 'template': 'T1',
                                     'required_template': 'T1', 'printed': True},
                                    {'id': 'CZ', 'parcel': 'PZ', 'paper_no': 'NZ', 'template': 'T1',
                                     'required_template': 'T1', 'printed': False}], 'other.json')
    other_digest = file_digest(other_export)
    assert other_digest != digest
    operations.receipts(db, receipt_file(tmp_path, [receipt(digest=other_digest)]), keys)
    assert (copy_row(db, 'C1')['status'], copy_row(db, 'C1')['reason']) == ('待核', 'receipt_export_mismatch')
    assert consumed(db) == 0


def test_voided_original_is_not_reported_valid(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_one(db, tmp_path, jobs=[{'copy_id': 'C1', 'template': 'T1', 'paper': 'A6'},
                                {'copy_id': 'C2', 'template': 'T1', 'paper': 'A6'}])
    keys = keys_file(tmp_path)
    file = spool(tmp_path, [{'id': 'C1', 'parcel': 'P1', 'paper_no': 'N1', 'template': 'T1',
                             'required_template': 'T1', 'printed': True}])
    digest = file_digest(file)
    operations.first(db, file, keys)
    operations.receipts(db, receipt_file(tmp_path, [receipt('C1', digest=digest)]), keys)
    operations.second(db, write(tmp_path, 'reprint.json', {'version': 'v1', 'rows': [
        {'id': 'R1', 'old_copy': 'C1', 'reason': 'damaged', 'approver': 'A',
         'at': '2026-01-01T10:00:00', 'expires': '2026-01-02T00:00:00', 'new_copy': 'C2', 'new_paper': 'N2'}]}), keys)
    data = report_data(db, tmp_path)
    assert data['evidence']['valid_copies'] == []
    assert [c['copy_id'] for c in data['evidence']['superseded_copies']] == ['C1']
    assert [c['copy_id'] for c in data['evidence']['pending_copies']] == ['C2']
    assert data['evidence']['pending_copies'][0]['reason'] == 'awaiting_physical_output'
