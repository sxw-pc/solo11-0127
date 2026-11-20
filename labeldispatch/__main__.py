import argparse,json,tempfile
from pathlib import Path
from . import operations,report,resources,disputes,billing
COMMANDS=['print-import', 'reprint-request', 'handoff-evaluate', 'handoff-settle','printer-plan','printer-cancel','carrier-claim','recall-resolve']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    for command in ('bill-import','bill-reference'):
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('bill-pay');arg.add_argument('--db',required=True);arg.add_argument('file')
    arg.add_argument('--policy',choices=billing.POLICIES,default='strict')
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    elif x.command=='bill-import':print(json.dumps(billing.import_bill(x.db,x.file),ensure_ascii=False))
    elif x.command=='bill-reference':print(json.dumps(billing.add_reference(x.db,x.file),ensure_ascii=False))
    elif x.command=='bill-pay':print(json.dumps(billing.pay(x.db,x.file,x.policy),ensure_ascii=False))
    else:
        fn=[operations.first,operations.second,operations.third,operations.settle,
            resources.plan,resources.cancel,disputes.claim,disputes.resolve][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
