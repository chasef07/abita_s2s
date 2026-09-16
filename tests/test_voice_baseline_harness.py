"""No-network checks for the audio-only transfer test harness."""

import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from livekit.agents.llm.utils import function_arguments_to_pydantic_model
from livekit.agents.simulation import SimulationMode

spec = importlib.util.spec_from_file_location(
    'voice_baseline_agent', Path(__file__).resolve().parents[1] / 'scripts/voice_baseline_agent.py'
)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class VoiceBaselineHarnessTests(unittest.IsolatedAsyncioTestCase):
    def job(self, mode):
        sim = None if mode is None else SimpleNamespace(simulation_mode=mode)
        return SimpleNamespace(simulation_context=lambda: sim)

    async def test_non_audio_calls_cannot_reach_session_or_phone(self):
        for mode in (None, SimulationMode.SIMULATION_MODE_TEXT):
            with patch.object(harness, 'get_job_context', return_value=self.job(mode)):
                with self.assertRaises(RuntimeError):
                    await harness.simulated_transfer(SimpleNamespace(), None)

    async def test_transfer_announces_and_closes_without_sip(self):
        events = []
        async def playout():
            events.append('announcement_finished')
        session = SimpleNamespace(
            generate_reply=Mock(return_value=SimpleNamespace(wait_for_playout=playout)),
            shutdown=Mock(side_effect=lambda **kwargs: events.append('shutdown')), once=Mock(),
        )
        ctx = SimpleNamespace(session=session, disallow_interruptions=Mock(), wait_for_playout=AsyncMock())
        control = SimpleNamespace(status='idle', attempts=0)
        with patch.object(harness, 'get_job_context', return_value=self.job(SimulationMode.SIMULATION_MODE_AUDIO)):
            output = json.loads(await harness.simulated_transfer(control, ctx))
        self.assertEqual(output['outcome'], 'accepted')
        self.assertEqual(events, ['announcement_finished', 'shutdown'])
        session.shutdown.assert_called_once_with(drain=True)
        self.assertEqual(control.attempts, 1)

    async def test_end_call_closes_without_sip(self):
        ctx = SimpleNamespace(session=SimpleNamespace(shutdown=Mock(), once=Mock()), disallow_interruptions=Mock())
        with patch.object(harness, 'get_job_context', return_value=self.job(SimulationMode.SIMULATION_MODE_AUDIO)):
            output = json.loads(await harness.simulated_end(SimpleNamespace(), ctx))
        self.assertEqual(output['outcome'], 'ended')
        ctx.session.shutdown.assert_called_once_with(drain=True)

    def test_session_close_ends_simulator_room(self):
        job = SimpleNamespace(add_shutdown_callback=Mock(), delete_room=AsyncMock(), shutdown=Mock())
        session = SimpleNamespace(once=Mock(), shutdown=Mock())
        with patch.object(harness, 'get_job_context', return_value=job):
            harness.close_simulated_call(SimpleNamespace(session=session))
        event, callback = session.once.call_args.args
        self.assertEqual(event, 'close')
        callback(SimpleNamespace())
        job.add_shutdown_callback.assert_called_once_with(job.delete_room)
        job.shutdown.assert_called_once_with(reason='simulated_call_complete')

    def test_end_call_schema_has_no_caller_arguments(self):
        bound = harness.simulated_end.__get__(SimpleNamespace())
        schema = function_arguments_to_pydantic_model(bound).model_json_schema()
        self.assertEqual(schema.get('properties', {}), {})


if __name__ == '__main__':
    unittest.main()
