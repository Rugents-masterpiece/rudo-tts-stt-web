"""Offline tests: isolated APP_HOME, no lifespan workers or live HTTP."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

SOURCE = Path(__file__).resolve().parents[1]


class VoiceSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        shutil.copytree(SOURCE / 'static', home / 'static')
        with patch.dict(os.environ, APP_HOME=str(home)):
            spec = importlib.util.spec_from_file_location('isolated_voice_app', SOURCE / 'app.py')
            self.web = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = self.web
            spec.loader.exec_module(self.web)
        for path in (self.web.DATA, self.web.JOBS_DIR, self.web.TTS_DIR, self.web.STT_DIR, self.web.CONV_DIR):
            path.mkdir(parents=True, exist_ok=True)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.web.app), base_url='http://offline')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.web.logging.getLogger('rudo-jobs').removeHandler(self.web.jh)
        self.web.jh.close()
        self.tmp.cleanup()

    async def test_selected_voice_and_seed_are_stored_and_forwarded(self):
        response = await self.client.post('/api/tts', json={'text': 'Ahoj', 'voice': 'melisko', 'seed': 7, 'normalize': False})
        self.assertEqual(response.status_code, 200)
        jid = response.json()['id']
        row = self.web.job_get(jid)
        params = json.loads(row['params'])
        self.assertEqual(params, {'text': 'Ahoj', 'voice': 'melisko', 'seed': 7, 'normalize': False})
        calls = []
        def manager(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, content=b'RIFFoffline-WAV')
        async with httpx.AsyncClient(transport=httpx.MockTransport(manager)) as manager_client:
            await self.web.run_tts(manager_client, row)
        self.assertEqual(calls, [{'input': 'Ahoj', 'voice': 'melisko', 'seed': 7, 'normalize': False}])
        final = self.web.job_get(jid)
        self.assertEqual(final['params'], row['params'])
        self.assertTrue(Path(final['result_path']).name.startswith('melisko-tts-'))

    async def test_metadata_defaults_invalid_voice_and_global_fifo(self):
        first = (await self.client.post('/api/tts', json={'text': 'Rudo'})).json()
        second = (await self.client.post('/api/tts', json={'text': 'Melisko', 'voice': 'melisko', 'seed': 0})).json()
        self.assertEqual(first['voice'], 'rudo')
        self.assertEqual(second['voice'], 'melisko')
        self.assertEqual(self.web.QUEUE.pending, [first['id'], second['id']])
        self.assertLess(first['sequence'], second['sequence'])
        for jid, voice in ((first['id'], 'rudo'), (second['id'], 'melisko')):
            self.assertEqual((await self.client.get('/api/job/' + jid)).json()['voice'], voice)
        history = (await self.client.get('/api/jobs')).json()['jobs']
        self.assertEqual({j['voice'] for j in history}, {'rudo', 'melisko'})
        # Old persisted rows have no voice/seed; default remains Rudo.
        self.web.job_set(first['id'], params={'text': 'legacy'})
        self.assertEqual((await self.client.get('/api/job/' + first['id'])).json()['voice'], 'rudo')
        calls = []
        def manager(request):
            if request.method == 'POST':
                calls.append(json.loads(request.content))
                return httpx.Response(200, content=b'RIFFlegacy')
            return httpx.Response(200, json={'status': 'healthy'})
        real_client = httpx.AsyncClient
        with patch.object(self.web.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(manager), **kw)):
            self.web.QUEUE.current = first['id']
            self.web.QUEUE.pending = [second['id']]
            status = (await self.client.get('/api/status')).json()['queue']
            self.assertEqual(status['current']['voice'], 'rudo')
            self.assertEqual(status['queued'][0]['voice'], 'melisko')
            await self.web.process_job(first['id'])
        self.assertEqual(calls, [{'input': 'legacy', 'normalize': True, 'voice': 'rudo'}])
        invalid = await self.client.post('/api/tts', json={'text': 'bad', 'voice': 'unknown'})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(len((await self.client.get('/api/jobs')).json()['jobs']), 2)

    async def test_ui_has_accessible_default_voice_selector(self):
        html = (await self.client.get('/')).text
        self.assertIn('for="ttsVoice">Hlas', html)
        self.assertIn('<option value="rudo" selected>Rudo</option>', html)
        self.assertIn('<option value="melisko">Melisko</option>', html)
        self.assertIn('Rudo 42', html)
        self.assertIn('Melisko 0', html)

    async def test_blank_seed_and_zero_override_use_selected_profile(self):
        for voice, seed in (('rudo', None), ('melisko', None), ('melisko', 0)):
            with self.subTest(voice=voice, seed=seed):
                result = (await self.client.post('/api/tts', json={'text': 'Test', 'voice': voice, 'seed': seed})).json()
                row = self.web.job_get(result['id'])
                calls = []
                def manager(request):
                    calls.append(json.loads(request.content))
                    return httpx.Response(200, content=b'RIFFtest')
                async with httpx.AsyncClient(transport=httpx.MockTransport(manager)) as cl:
                    await self.web.run_tts(cl, row)
                expected = {'input': 'Test', 'voice': voice, 'normalize': True}
                if seed is not None:
                    expected['seed'] = seed
                self.assertEqual(calls, [expected])
                self.assertEqual(json.loads(self.web.job_get(result['id'])['params'])['seed'], seed)

    async def test_stt_and_conversion_keep_shared_queue_and_contracts(self):
        first = (await self.client.post('/api/tts', json={'text': 'TTS', 'voice': 'melisko'})).json()
        stt = (await self.client.post('/api/stt', files={'files': ('speech.wav', b'RIFFsource', 'audio/wav')})).json()['jobs'][0]
        def manager(request):
            self.assertTrue(str(request.url).endswith('/v1/audio/transcriptions'))
            return httpx.Response(200, json={'text': 'prepis', 'duration_s': 3})
        # Avoid actual FFmpeg invocation; existing upload canonicalization is unchanged.
        with patch.object(self.web, '_ffmpeg', side_effect=lambda argv, timeout: Path(argv[-1]).write_bytes(b'RIFFcanonical')):
            async with httpx.AsyncClient(transport=httpx.MockTransport(manager)) as cl:
                await self.web.run_stt(cl, self.web.job_get(stt['id']))
        detail = (await self.client.get('/api/job/' + stt['id'])).json()
        self.assertEqual(detail['transcript'], 'prepis')
        self.assertIsNone(detail['voice'])
        src = self.web.TTS_DIR / 'melisko-tts-source.wav'
        src.write_bytes(b'RIFFsource')
        self.web.job_set(first['id'], result_path=str(src), status='completed')
        converted = (await self.client.post('/api/jobs/' + first['id'] + '/convert', json={'wav_path': str(src), 'format': 'mp3'})).json()
        self.assertEqual(self.web.QUEUE.pending, [first['id'], stt['id'], converted['id']])
        with patch.object(self.web, '_ffmpeg') as ffmpeg:
            self.web.run_convert(self.web.job_get(converted['id']))
        args, timeout = ffmpeg.call_args.args
        self.assertIn('libmp3lame', args)
        self.assertEqual(timeout, 600)
        self.assertIsNone((await self.client.get('/api/job/' + converted['id'])).json()['voice'])
