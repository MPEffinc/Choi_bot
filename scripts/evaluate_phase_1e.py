"""Opt-in, capped real API comparison. Outputs responses/metrics, never prompts/keys.

Run screen (28 requests), inspect results, then confirm (12 requests) with a chosen
candidate. Use the SAME ledger for both stages. No automatic retry of an experiment
or interrupted request; Router retains its existing within-request retry policy.
"""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.phase_1e_candidates import ROOT, build
from bot.llm.contracts import LLMError
from bot.llm.gemini import GeminiAdapter
from bot.llm.router import LLMRouter, task_policies
from bot.settings import load_settings

MODEL = 'gemini-3.5-flash-lite'
LIMIT = 40


def plan(stage, chosen):
    if stage == 'screen':
        return [(case, candidate) for i, case in enumerate(('R1','R2','R3','R4','H1','H2','H3'))
                for candidate in ('ABCD'[i % 4:] + 'ABCD'[:i % 4])]
    return [(case, candidate) for i, case in enumerate(('H4','H5','H6','CMD_question','CMD_info','CMD_detail'))
            for candidate in (('A',chosen) if i % 2 == 0 else (chosen,'A'))]


async def run(args):
    cases = {c['id']:c for c in json.loads((ROOT/'tests/fixtures/phase_1e_cases.json').read_text())['cases']}
    work = plan(args.stage,args.chosen)
    if not args.live:
        print(f'Offline: {len(work)} logical requests planned; model={MODEL}; defaults unchanged')
        for cid,candidate in work:
            build(candidate,cases[cid])
        return
    if not args.ledger:
        raise SystemExit('--live requires --ledger (same path for both stages)')
    # Exclusive process lock + reservations fsynced before network, including failures.
    fd = os.open(args.ledger,os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'r+',encoding='utf-8') as ledger:
        fcntl.flock(ledger,fcntl.LOCK_EX|fcntl.LOCK_NB)
        rows=[json.loads(line) for line in ledger if line.strip()]
        starts=[r for r in rows if r['event']=='start']
        results=[r for r in rows if r['event']=='result']
        if len(starts)!=len(results) or any(r.get('error') for r in results):
            raise SystemExit('Stopped ledger: incomplete request or prior error; no further calls')
        if any(r['stage']==args.stage for r in starts):
            raise SystemExit('Stage already attempted; refusing duplicate experiment')
        if args.stage=='confirm' and len(starts)!=28:
            raise SystemExit('Confirm requires the completed 28-request screening ledger')
        if len(starts)+len(work)>LIMIT:
            raise SystemExit('40-request budget exceeded')
        def record(row):
            ledger.write(json.dumps(row,ensure_ascii=False)+'\n');ledger.flush();os.fsync(ledger.fileno())
        settings=load_settings(ROOT/'ini.env')
        provider=GeminiAdapter(settings.api_keys,MODEL,key_ids=settings.key_ids)
        router=LLMRouter({'gemini':provider},task_policies(MODEL))
        try:
            for cid,candidate in work:
                request=build(candidate,cases[cid])
                fingerprint=hashlib.sha256(json.dumps([request.system_instruction,
                    [(m.role,m.content) for m in request.messages]],ensure_ascii=False).encode()).hexdigest()
                record(dict(event='start',stage=args.stage,case=cid,candidate=candidate,
                            request_id=request.request_id,sha256=fingerprint,
                            utc=datetime.now(timezone.utc).isoformat(),generation_options={},model=MODEL))
                try:
                    response=await router.generate(request)
                except LLMError as error:
                    record(dict(event='result',case=cid,candidate=candidate,error=error.error_type,
                                status=error.status_code,attempt_count=error.attempt_count,
                                metric=router.metrics[-1]))
                    print(f'{cid}/{candidate}: STOP {error.error_type}; attempts={error.attempt_count}',flush=True)
                    return
                record(dict(event='result',case=cid,candidate=candidate,**asdict(response),
                            metric=router.metrics[-1]))
                print(f'{cid}/{candidate}: saved; attempts={response.attempt_count}',flush=True)
        finally:
            await router.aclose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--ledger',type=Path)
    parser.add_argument('--stage',choices=['screen','confirm'],default='screen')
    parser.add_argument('--chosen',choices=['B','C','D'],default='C')
    asyncio.run(run(parser.parse_args()))
