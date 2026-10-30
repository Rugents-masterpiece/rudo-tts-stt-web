"""Actual-code deterministic defect characterization; no model/GPU/process startup.
Run: python3 test_lifecycle.py [-v] [--require-recovery]
Default asserts current defects (PASS means defect confirmed).
--require-recovery additionally asserts required behavior (expected RED unpatched).
"""
import ast
import hashlib
import importlib.util
import json
import pathlib
import queue
import sys
import threading
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True  # Keep production imports strictly read-only.
ROOT = pathlib.Path('/home/rugent/rtx3050-profile-prototype/production-candidate')
OUT = pathlib.Path(__file__).resolve().parent
REQUIRE_RECOVERY = '--require-recovery' in sys.argv
if REQUIRE_RECOVERY:
    sys.argv.remove('--require-recovery')
EVIDENCE = []

# Stub only web dependencies, not Manager/Job or adapter implementations.
class FakeAPI:
    def __init__(self, **kw): pass
    def __getattr__(self, name): return lambda *a, **kw: lambda f: f
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)
fastapi = types.ModuleType('fastapi')
fastapi.FastAPI = FakeAPI
fastapi.UploadFile = object
fastapi.File = lambda *a, **kw: None
fastapi.HTTPException = HTTPException
responses = types.ModuleType('fastapi.responses')
responses.Response = object
pydantic = types.ModuleType('pydantic')
pydantic.BaseModel = object
sys.modules.update({'fastapi': fastapi, 'fastapi.responses': responses, 'pydantic': pydantic})

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

# Disallow all process creation; actual adapters imported but never constructed normally.
with patch('subprocess.Popen', side_effect=AssertionError('process startup forbidden')):
    adapters = load('adapters', ROOT / 'adapters.py')
    manager = load('actual_manager', ROOT / 'manager_app.py')

class FakeWorker:
    def __init__(self, state='cpu_ready', alive=True):
        self.state, self.alive = state, alive
        self.activations = self.deactivations = self.restarts = 0
    def health(self): return {'state': self.state, 'alive': self.alive}
    def activate_gpu(self):
        self.activations += 1
        self.state = 'gpu_ready'
        return {'ok': True, 'state': self.state}
    def deactivate_gpu(self):
        self.deactivations += 1
        self.state = 'cpu_ready'
        return {'ok': True, 'state': self.state}
    def restart(self):
        self.restarts += 1
        self.alive, self.state = True, 'cpu_ready'
    def retire_gpu_worker(self): return self.deactivate_gpu()

class FakeRPC:
    def __init__(self):
        self.calls = []
        self.p = types.SimpleNamespace(poll=lambda: None)
    def call(self, request, *args):
        self.calls.append(request['op'])
        return {'ok': False, 'state': 'error', 'error': 'CUDA error: device-side assert triggered'}

def tts_adapter():
    t = adapters.TTSWorkerAdapter.__new__(adapters.TTSWorkerAdapter)
    t.w, t.state, t.last = FakeRPC(), 'gpu_ready', {}
    t.python, t.app, t.restarts = 'MOCK', 'MOCK', 0
    def restart():
        t.restarts += 1
        t.state = 'cpu_ready'
    t.restart = restart
    return t

def fresh(tts=None):
    with patch.object(threading.Thread, 'start'):
        m = manager.Manager(idle=1)
    m.workers = {'tts': tts or FakeWorker(), 'retrieval': FakeWorker(), 'stt': FakeWorker()}
    return m

def run_job(m, profile, fn):
    j = manager.Job(profile, fn, manager.time.time() + 5)
    m.q.put(j)
    m.q.put(None)
    m._loop()
    return j

def one_idle(m):
    # Execute exactly one real idle iteration without wallclock waits.
    with patch.object(manager, 'time', types.SimpleNamespace(time=lambda: 1000, sleep=lambda _: setattr(m, 'running', False))):
        m._idle_loop()
    m.running = True

def extract_worker_function(name):
    tree = ast.parse((ROOT / 'chatterbox_worker.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / 'chatterbox_worker.py'), 'exec')

class DefectTests(unittest.TestCase):
    def test_alive_error_blocks_retrieval_and_endpoint_returns_503(self):
        t = tts_adapter()
        m = fresh(t)
        m.active_profile = 'tts'
        failed = run_job(m, 'tts', lambda: t.synth('mock input'))
        self.assertIsInstance(failed.error, RuntimeError)
        self.assertEqual(t.state, 'error')
        self.assertTrue(t.health()['alive'])
        executed = []
        for _ in range(2):
            j = run_job(m, 'retrieval', lambda: executed.append(True))
            self.assertIsInstance(j.error, RuntimeError)
        self.assertEqual(executed, [])
        self.assertEqual(m.active_profile, 'tts')
        self.assertEqual(m.transition, 'deactivating')
        self.assertEqual(m.state, 'error')
        self.assertEqual(t.restarts, 0)
        with patch.object(manager, 'M', m), patch.object(m, 'submit', side_effect=lambda p, f: (_ for _ in ()).throw(run_job(m, p, f).error)):
            with self.assertRaises(HTTPException) as error:
                manager.embeddings(types.SimpleNamespace(model_dump=lambda: {}))
        self.assertEqual(error.exception.status_code, 503)
        EVIDENCE.append({'case': 'alive_error_blocks_retrieval', 'snapshot': m.snapshot(), 'tts_rpc_ops': t.w.calls, 'retrieval_body_executed': bool(executed), 'endpoint_status': 503})
        if REQUIRE_RECOVERY:
            self.assertGreater(t.restarts, 0, 'fatal alive worker must be retired/replaced')

    def test_idle_error_is_not_recovered(self):
        t = tts_adapter(); t.state = 'error'
        m = fresh(t); m.active_profile = 'tts'; m.state = 'error'; m.last_used['tts'] = 0
        one_idle(m)
        self.assertEqual(t.restarts, 0)
        self.assertEqual(m.active_profile, 'tts')
        self.assertEqual(m.transition, 'error')
        EVIDENCE.append({'case': 'idle_error_not_recovered', 'snapshot': m.snapshot()})

    def test_same_profile_error_skips_activation(self):
        t = tts_adapter(); t.state = 'error'
        m = fresh(t); m.active_profile = 'tts'
        j = run_job(m, 'tts', lambda: t.synth('mock input'))
        self.assertIsNotNone(j.error)
        self.assertEqual(t.w.calls, ['synth'])
        self.assertEqual(t.restarts, 0)

    def test_idle_offload_overlaps_request_and_clears_new_owner(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingWorker(FakeWorker):
            def deactivate_gpu(self):
                self.deactivations += 1
                self.state = 'offloading'
                entered.set()
                if not release.wait(3): raise TimeoutError('test barrier')
                self.state = 'cpu_ready'
                return {'ok': True, 'state': self.state}
        t = BlockingWorker('gpu_ready')
        m = fresh(t); m.active_profile = 'tts'; m.last_used['tts'] = 0
        clock = types.SimpleNamespace(time=lambda: 1000, sleep=lambda _: setattr(m, 'running', False))
        with patch.object(manager, 'time', clock):
            idle = threading.Thread(target=m._idle_loop)
            idle.start()
            self.assertTrue(entered.wait(2))
            m.running = True
            observed = []
            try:
                j = run_job(m, 'tts', lambda: observed.append({'state_at_request': t.state, 'active_job': m.active_job.id}))
            finally:
                release.set(); idle.join(3)
        self.assertFalse(idle.is_alive())
        self.assertIsNone(j.error)
        self.assertEqual(observed[0]['state_at_request'], 'offloading')
        self.assertEqual(t.activations, 0)
        self.assertIsNone(m.active_profile)
        self.assertEqual(m.state, 'idle')
        EVIDENCE.append({'case': 'idle_request_race', 'request_observation': observed, 'snapshot': m.snapshot(), 'activation_calls': t.activations})
        if REQUIRE_RECOVERY:
            self.assertEqual(observed[0]['state_at_request'], 'gpu_ready', 'request must not overlap idle offload')

    def test_idle_unhandled_health_error_terminates_loop(self):
        t = FakeWorker()
        t.health = lambda: (_ for _ in ()).throw(RuntimeError('poll race'))
        m = fresh(t); m.active_profile = 'tts'
        with self.assertRaisesRegex(RuntimeError, 'poll race'):
            one_idle(m)

    def test_idle_restarts_dead_worker_even_with_active_job(self):
        t = FakeWorker('error', alive=False)
        m = fresh(t); m.active_profile = 'tts'
        m.active_job = manager.Job('tts', lambda: None, 2000)
        one_idle(m)
        self.assertEqual(t.restarts, 1)
        self.assertIsNotNone(m.active_job)
        self.assertIsNone(m.active_profile)
        EVIDENCE.append({'case': 'idle_restart_during_active_job', 'restart_calls': t.restarts, 'job_still_active': True})

    def test_actual_worker_deactivate_failure_sets_error(self):
        tree = ast.parse((ROOT / 'chatterbox_worker.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Worker')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'deactivate')
        env = {'move_model': lambda *a: (_ for _ in ()).throw(RuntimeError('CUDA error: device-side assert triggered'))}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT / 'chatterbox_worker.py'), 'exec'), env)
        w = types.SimpleNamespace(state='gpu_ready', model=object())
        with self.assertRaisesRegex(RuntimeError, 'device-side assert'):
            env['deactivate'](w)
        self.assertEqual(w.state, 'error')

    def test_dead_worker_control_does_restart(self):
        t = FakeWorker('error', alive=False)
        m = fresh(t); m.active_profile = 'tts'
        one_idle(m)
        self.assertEqual(t.restarts, 1)
        self.assertIsNone(m.active_profile)
        self.assertEqual(m.state, 'idle')

    def test_actual_worker_main_cuda_runtime_error_preserves_ready_and_continues(self):
        class OOM(Exception): pass
        class W:
            state = 'gpu_ready'; load_info = {}
            def snapshot(self): return {'state': self.state}
            def generate(self, request): raise RuntimeError('CUDA error: device-side assert triggered')
        requests = iter([{'op': 'synth'}, {'op': 'state'}, None]); sent = []
        env = {'Worker': W, 'send': sent.append, 'recv': lambda: next(requests), 'torch': types.SimpleNamespace(cuda=types.SimpleNamespace(OutOfMemoryError=OOM)), 'logging': types.SimpleNamespace(exception=lambda *a: None)}
        exec(extract_worker_function('main'), env)
        env['main']()
        self.assertFalse(sent[1]['ok'])
        self.assertEqual(sent[1]['state'], 'gpu_ready')
        self.assertTrue(sent[2]['ok'])
        self.assertEqual(sent[2]['state'], 'gpu_ready')
        EVIDENCE.append({'case': 'actual_worker_main_generic_cuda_error', 'responses': sent, 'method': 'AST-extracted original main, mock Worker/torch/protocol only'})
        if REQUIRE_RECOVERY:
            self.assertEqual(sent[1].get('fatal_error'), True, 'fatal CUDA errors must be explicitly classified')

    def test_actual_adapter_synth_drops_error_details_from_health(self):
        t = tts_adapter()
        with self.assertRaises(RuntimeError): t.synth('mock input')
        self.assertEqual(t.state, 'error')
        self.assertNotIn('error', t.health())
        self.assertTrue(t.health()['alive'])

if __name__ == '__main__':
    before = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in ['manager_app.py', 'adapters.py', 'chatterbox_worker.py']}
    with patch('subprocess.Popen', side_effect=AssertionError('process startup forbidden')), patch('subprocess.check_output', side_effect=AssertionError('GPU query forbidden')), patch('subprocess.run', side_effect=AssertionError('external command forbidden')):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(DefectTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    after = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in before}
    payload = {'mode': 'required-recovery-red' if REQUIRE_RECOVERY else 'defect-characterization', 'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'source_hashes_before': before, 'source_hashes_after': after, 'production_unchanged': before == after, 'evidence': EVIDENCE}
    path = OUT / ('red-evidence.json' if REQUIRE_RECOVERY else 'evidence.json')
    path.write_text(json.dumps(payload, indent=2) + '\n')
    print('EVIDENCE', path)
    sys.exit(0 if result.wasSuccessful() and before == after else 1)
