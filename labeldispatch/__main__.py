import argparse,json,tempfile
from pathlib import Path
from . import operations,report,resources,disputes
COMMANDS=['print-import', 'reprint-request', 'handoff-evaluate', 'handoff-settle','printer-plan','printer-cancel','carrier-claim','recall-resolve',
          'printer-receipt','physical-witness']
KEYED={'print-import','reprint-request','printer-receipt'}
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
        if command in KEYED:
            arg.add_argument('--keys',help='runtime-controlled printer key file (JSON)')
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    x=parser.parse_args()
    if x.command=='demo':
        # Demo intentionally has no key configuration: legacy queue-claim
        # evidence level, explicitly labelled as such in the report.
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    elif x.command=='printer-receipt':print(json.dumps(operations.receipts(x.db,x.file,x.keys),ensure_ascii=False))
    elif x.command=='physical-witness':print(json.dumps(operations.witnesses(x.db,x.file),ensure_ascii=False))
    else:
        fn=[operations.first,operations.second,operations.third,operations.settle,
            resources.plan,resources.cancel,disputes.claim,disputes.resolve][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
