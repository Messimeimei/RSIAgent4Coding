#!/usr/bin/env python3
"""Unpaid, real BitFun/Docker BRS -> DRS -> frozen baseline -> Pier smoke.

The stub exercises lifecycle transitions; it does not solve or score dateutil.
All model endpoints are replaced before creating the job.
"""

import argparse
import asyncio
import copy
import json
import os
import re
import shlex
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.smoke_deepswe_bitfun import ModelStub


def content(message):
    value = message.get('content', '')
    return '\n'.join(p.get('text', '') for p in value if isinstance(p, dict)) if isinstance(value, list) else str(value)


def publish(path, value):
    return (f"p=Path({path!r}); p.parent.mkdir(parents=True,exist_ok=True); "
            f"t=p.with_suffix('.pending'); t.write_text({value!r}); t.replace(p); ")


class RSIStub(ModelStub):
    lock = threading.Lock()
    requests_seen = 0
    actions = {}
    audit = []
    targets = 0
    reviews = 0
    curriculum_turns = 0
    drs_reviews = 0
    trace_dir = None

    def do_POST(self):
        try:
            self.respond()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:
            type(self).audit.append({'error': repr(error)})
            self.json_reply(400, {'error': {'message': str(error)}})

    def plan(self, prompt, messages):
        control_match = re.search(r'BASELINE_CONTEXT: (\{[^\n]+\})', prompt)
        script = 'from pathlib import Path; import json, subprocess; '
        if control_match:
            control = json.loads(control_match[1])
            if control['role'] == 'actor':
                type(self).targets += 1
                number = type(self).targets
                stage = f'target-{number}'
                # Fresh target environments must not inherit earlier code.
                script += "assert not Path('/app/rsi-smoke.txt').exists(); "
                script += "memory=Path('/home/user/.memory'); assert len(list(memory.glob('lesson-brs*.md')))==3; "
                if number > 1:
                    script += "assert (memory/'lesson-drs.md').is_file(); assert (memory/'lesson-target-1.md').is_file(); "
                script += f"Path('/app/rsi-smoke.txt').write_text({stage!r}); "
                script += ("subprocess.run(['git','add','rsi-smoke.txt'],check=True); "
                    "subprocess.run(['git','-c','user.email=smoke@test','-c','user.name=Smoke',"
                    "'commit','-qm','RSI lifecycle smoke'],check=True); ")
                probe = "cd /app && python3 -c " + shlex.quote(
                    f"from pathlib import Path; assert Path('rsi-smoke.txt').read_text()=={stage!r}; print('PASS')")
                report = dict(kind='actor_done', handoff_id=control['handoff_id'], final=True,
                    summary='Controlled lifecycle smoke', candidate_head=None,
                    checks=[dict(desc='Committed marker can be read', probe=probe)])
                script += f"report={report!r}; report['candidate_head']=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(); "
                script += publish(control['report_path'], '').replace("t.write_text('')", 't.write_text(json.dumps(report))')
            else:
                type(self).reviews += 1
                number = type(self).reviews
                verdict = 'FAIL' if number == 1 else 'PASS'
                stage = f'target-review-{number}-{verdict}'
                script += f"assert Path('/app/rsi-smoke.txt').read_text()=='target-{number}'; "
                script += "assert not Path('/home/user/.memory').exists(); "
                report = dict(kind='verifier_report', handoff_id=control['handoff_id'], final=True,
                    candidate_id=control['candidate_id'], verdict=verdict,
                    report='Controlled independent smoke verdict',
                    findings=['Controlled missing requirement'] if verdict == 'FAIL' else [],
                    evidence=['Read the submitted marker independently'], evidence_requests=[])
                script += publish(control['report_path'], json.dumps(report))
            return stage, script

        remote = re.search(r'/logs/agent/handoffs/[a-f0-9]+\.json', prompt)[0]
        envelope = re.search(r'\{"kind":"rsi_done"[^\n]+?\}', prompt)
        if 'ACTOR AGENT — FINAL MEMORY DISTILLATION.' in prompt:
            stage = 'distillation'
            assert 'RSI_SMOKE_STAGE: actor' in json.dumps(messages) or 'RSI_SMOKE_STAGE: target-' in json.dumps(messages)
            script += ("project=Path('/home/user/evolution_project'); "
                "label=(project/'practice-id').read_text() if project.exists() else Path('/app/rsi-smoke.txt').read_text(); "
                "memory=Path('/home/user/.memory'); memory.mkdir(exist_ok=True); "
                "(memory/('lesson-'+label+'.md')).write_text('Observed controlled experience: '+label); ")
        elif 'ACTOR AGENT — TERMINAL MEMORY RECONCILIATION.' in prompt:
            stage = 'reconciliation'
            assert 'RSI_SMOKE_STAGE: distillation' in json.dumps(messages)
            script += "memory=Path('/home/user/.memory'); assert list(memory.glob('lesson-*.md')); "
        elif 'Write a complete free-form learning diagnosis' in prompt:
            stage = 'diagnosis'
            assert 'RSI_SMOKE_STAGE: reconciliation' in json.dumps(messages)
            script += publish('/home/user/learning_diagnosis.md', 'Controlled experience; verify generalization independently.')
        elif 'PERSISTENT PHASE-1 PARALLEL SEARCH' in prompt:
            stage = 'wave'
            projects = []
            for name in ('brs-a', 'brs-b', 'brs-c'):
                instruction = f'RSI_SMOKE_PROJECT: {name}\nImplement answer.py with answer() returning 42 in /home/user/evolution_project; keep every deliverable there.'
                projects.append(dict(id=name, instruction=instruction))
                script += publish(f'/home/user/evolution_wave/{name}/evolution_project/practice-id', name)
                script += publish(f'/home/user/evolution_wave/{name}/evolution_project/answer.py', 'def answer(): raise NotImplementedError\n')
            script += publish('/home/user/phase1_wave.json', json.dumps(dict(decision='WAVE', rationale='Controlled parallel lifecycle', projects=projects)))
        elif 'CURRICULUM AGENT' in prompt and '/home/user/project_next.md' in prompt:
            type(self).curriculum_turns += 1
            stage = f'drs-curriculum-{type(self).curriculum_turns}'
            if type(self).curriculum_turns == 1:
                script += publish('/home/user/evolution_project/practice-id', 'drs')
                script += publish('/home/user/evolution_project/answer.py', 'def answer(): raise NotImplementedError\n')
                script += publish('/home/user/project_next.md', 'DECISION: PROJECT\nRSI_SMOKE_PROJECT: drs\nImplement answer.py with answer() returning 42 in /home/user/evolution_project. Keep all deliverables there.')
            else:
                assert 'RSI_SMOKE_STAGE: drs-curriculum-1' in json.dumps(messages)
                script += publish('/home/user/project_next.md', 'DECISION: READY_FOR_TARGET\nControlled practice completed; test the target again.')
        elif envelope:
            stage = 'actor'
            script += "project=Path('/home/user/evolution_project'); assert (project/'practice-id').is_file(); "
            script += publish('/home/user/evolution_project/answer.py', 'def answer(): return 42\n')
            script += publish('/home/user/actor_handoff.md', 'STATUS: SUBMIT\nImplementation is available in the project directory.')
        else:
            project = re.search(r'RSI_SMOKE_PROJECT: ([a-z-]+)', prompt)[1]
            verdict = 'FAIL' if project == 'brs-b' else 'PASS'
            if project == 'drs':
                type(self).drs_reviews += 1
                verdict = 'UNVERIFIED' if type(self).drs_reviews == 1 else 'PASS'
            stage = f'practice-review-{project}-{verdict}'
            script += "assert not Path('/home/user/.memory').exists(); "
            script += ("source=Path('/home/user/evolution_project/answer.py').read_text(); "
                       "ns={}; exec(source,ns); assert ns['answer']()==42; ")
            script += publish(remote, f'Independent controlled check of answer().\nVERDICT: {verdict}\n'
                + ('EVIDENCE REQUEST: provide a reproducible answer() probe.\n' if verdict == 'UNVERIFIED' else ''))
            return stage, script
        done = json.loads(envelope[0])
        if 'independent-channel probes among your checks' in prompt:
            done['checks'] = [dict(desc='Answer function returns 42', probe='cd /home/user/evolution_project && python3 -B -c '+shlex.quote("from answer import answer; assert answer()==42; print('PASS')"))]
        script += publish(remote, json.dumps(done))
        return stage, script

    def respond(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        messages = body.get('messages', [])
        native_tools = any(t.get('function', {}).get('name') == 'ExecCommand'
                           for t in body.get('tools', []))
        index = next((i for i in range(len(messages)-1, -1, -1)
            if native_tools and messages[i].get('role') == 'user' and '/logs/agent/handoffs/' in content(messages[i])
            and any(marker in content(messages[i]) for marker in (
                'BITFUN COMPLETION TRANSPORT:', 'BASELINE_CONTEXT:', 'CODING ISOLATION:'))), None)
        prompt = content(messages[index]) if index is not None else ''
        results = [m for m in messages[index+1:] if m.get('role') == 'tool'] if index is not None else []
        with self.lock:
            type(self).requests_seen += 1
            number = type(self).requests_seen
            if number > 180:
                raise RuntimeError('RSI smoke request ceiling reached')
            if self.trace_dir:
                (self.trace_dir/f'{number:03d}.json').write_text(json.dumps(body))
            stage, script = 'auxiliary', None
            if index is not None:
                path = re.search(r'/logs/agent/handoffs/[a-f0-9]+\.json', prompt)[0]
                if path not in self.actions:
                    self.actions[path] = self.plan(prompt, messages[:index])
                stage, script = self.actions[path]
            self.audit.append(dict(request=number, stage=stage, results=len(results)))
        message = dict(role='assistant', content='RSI_SMOKE_STAGE: '+stage)
        finish = 'stop'
        if script and not results:
            message['tool_calls'] = [dict(index=0, id=f'rsi-smoke-{number}', type='function',
                function=dict(name='ExecCommand', arguments=json.dumps(dict(
                    cmd='python3 -c '+shlex.quote(script), yield_time_ms=10000))))]
            finish = 'tool_calls'
        elif results:
            time.sleep(3)
        envelope = dict(id=f'rsi-smoke-{number}', created=int(time.time()), model=body.get('model'))
        usage = dict(prompt_tokens=120, completion_tokens=20, total_tokens=140)
        if body.get('stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            for delta, stop in ((message, None), ({}, finish)):
                chunk = dict(**envelope, object='chat.completion.chunk', choices=[dict(index=0, delta=delta, finish_reason=stop)])
                if stop:
                    chunk['usage'] = usage
                self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            self.close_connection = True
        else:
            self.json_reply(200, dict(**envelope, object='chat.completion', usage=usage,
                choices=[dict(index=0, message=message, finish_reason=finish)]))


async def smoke(args, endpoint):
    from pier.job import Job
    from run_deepswe import load_job
    config = load_job(ROOT / 'config/deepswe/rsi-bitfun.yaml', name=args.name, mode='rsi')
    config.jobs_dir = ROOT / 'results/deepswe/smoke'
    cfg = config.agents[0]
    cfg.model_name = 'rsi-mock'
    cfg.kwargs.update(brs_project_budget=3, parallel=2, learning_timeout_sec=180,
        target_learning_timeout_sec=240, total_timeout_sec=240,
        checkpoint_repair_timeout_sec=10, command_timeout_sec=30)
    for key in ('bitfun_config', 'verifier_bitfun_config', 'curriculum_bitfun_config'):
        value = copy.deepcopy(cfg.kwargs[key])
        ai = value['ai']
        model = ai['models'][0]
        model.update(id='rsi-mock', name='rsi-mock', model_name='rsi-mock', base_url=endpoint,
                     api_key='${DEEPSWE_SMOKE_KEY}', max_tokens=4096, reasoning_mode='disabled')
        model.pop('reasoning_effort', None)
        ai['default_models'] = dict(primary='rsi-mock', fast='rsi-mock')
        cfg.kwargs[key] = value
    config.environment.kwargs['inference_ports'] = [args.port]
    destination = config.jobs_dir / config.job_name
    if destination.exists():
        raise FileExistsError(destination)
    job = await Job.create(config)
    RSIStub.trace_dir = destination/'stub-requests'
    RSIStub.trace_dir.mkdir()
    result = await job.run()
    trials = [p.parent for p in destination.glob('*/result.json')]
    checks = dict(one_trial=len(trials)==1, no_infrastructure_error=result.stats.n_errored_trials==0,
                  stub_no_errors=not any('error' in record for record in RSIStub.audit))
    if len(trials) == 1:
        trial = trials[0]
        def read(name):
            path = trial / name
            return json.loads(path.read_text()) if path.exists() else {}
        outcome = read('rsi/result.json')
        brs = [read(f'rsi/phase1/episodes/ep{i:03d}/outcome.json') for i in range(1,4)]
        phase2 = read('rsi/phase2/phase-result.json')
        frozen = trial / 'rsi/phase2/memory_frozen'
        patch = trial / 'artifacts/model.patch'
        checks.update(completed_all_stages=outcome.get('status')=='completed' and outcome.get('phase3_verdict')=='PASS',
            brs_fail_is_learning=[v.get('terminal_outcome') for v in brs]==['PASS','FAIL','PASS'],
            fresh_target_retry=phase2.get('target_cycles')==2,
            drs_one_practice=phase2.get('practice_projects')==1,
            all_brs_lessons_crossed_boundary=len(list(frozen.glob('lesson-brs*.md')))==3,
            drs_lessons_crossed_boundary=(frozen/'lesson-drs.md').exists() and (frozen/'lesson-target-1.md').exists(),
            final_memory_unchanged=outcome.get('frozen_memory_unchanged') is True,
            final_patch_from_fresh_actor=patch.exists() and '+target-3' in patch.read_text(),
            official_tests_executed=read('verifier/reward.json').get('p2p_passed',0)>0,
            evidence_loop_used=RSIStub.drs_reviews==2,
            all_role_publications=len(RSIStub.actions)>=24)
    receipt = dict(status='passed' if all(checks.values()) else 'failed', paid_model_calls=0,
        validation_kind='real_native_sessions_and_docker_complete_rsi_with_local_model_stub',
        checks=checks, requests=RSIStub.audit, job_path=str(destination))
    (destination/'smoke-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps({k:v for k,v in receipt.items() if k!='requests'}, indent=2), flush=True)
    return 0 if all(checks.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--bind-host', default='169.254.30.1')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if Path(args.name).name != args.name or args.name in {'.', '..'}:
        parser.error('name must be one directory component')
    os.chdir(ROOT)
    os.umask(0o077)
    os.environ['DEEPSWE_SMOKE_KEY'] = 'local-unpaid-model'
    with ThreadingHTTPServer((args.bind_host,args.port),RSIStub) as server:
        args.port=server.server_port
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            return asyncio.run(smoke(args,f'http://{args.bind_host}:{args.port}/v1'))
        finally:
            server.shutdown()
            worker.join(timeout=5)


if __name__=='__main__':
    raise SystemExit(main())
