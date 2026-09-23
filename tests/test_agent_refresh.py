from __future__ import annotations
import asyncio
from pathlib import Path
import unittest

from omodachi_core.agent import AgentState, AgentStatus, DefaultAgentCapabilities, HerdrStatusSnapshot, ProbeStatus
from omodachi_core.bootstrap import create_service
from omodachi_core.hub import Hub


class SequenceProbe:
    def __init__(self, sequence, delay=0.0): self.sequence=list(sequence); self.delay=delay; self.calls=0; self.active=0; self.max_active=0
    def inspect(self):
        self.calls += 1; self.active += 1; self.max_active=max(self.max_active,self.active)
        try:
            import time
            if self.delay: time.sleep(self.delay)
            result=self.sequence[min(self.calls-1,len(self.sequence)-1)]
            if isinstance(result,Exception): raise result
            return result
        finally: self.active -= 1

def cap(status, *, running=True, exists=True, pane=True, kind='codex'):
    return DefaultAgentCapabilities('codex', ProbeStatus.AVAILABLE, frozenset({'codex'}),
        ProbeStatus.AVAILABLE if running else ProbeStatus.NOT_RUNNING, exists,
        'pane-1' if pane else None, pane, AgentStatus(status),
        ProbeStatus.AVAILABLE if exists else ProbeStatus.MISSING,
        ProbeStatus.AVAILABLE if pane else ProbeStatus.UNREADABLE, actual_kind=kind)

def herdr(status='working', running=True):
    return HerdrStatusSnapshot(True, running, running, frozenset({'codex'}),
        (AgentState('default','codex',AgentStatus(status),'pane-1',True),) if running else ())


class AgentRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_probe_updates_working_blocked_done_and_broadcasts(self):
        probe=SequenceProbe([(cap('working'),herdr('working')),(cap('blocked'),herdr('blocked')),(cap('done'),herdr('done'))])
        hub=Hub(); service=create_service(hub,demo=True,agent_probe=probe)
        cursor=hub.event_cursor
        self.assertTrue(await service.refresh_agent_now())
        self.assertEqual(service.state('a')['agent']['status'],'working')
        self.assertTrue(await service.refresh_agent_now())
        self.assertEqual(service.state('a')['agent']['status'],'blocked')
        self.assertTrue(await service.refresh_agent_now())
        self.assertEqual(service.state('a')['agent']['status'],'done')
        events=[e for e in hub.events_since(cursor,device_id='a') if e.type=='agent.changed']
        self.assertGreaterEqual(len(events),3)
        self.assertEqual(probe.max_active,1)

    async def test_probe_is_non_overlapping_and_failure_does_not_block_hub(self):
        probe=SequenceProbe([(cap('working'),herdr('working')),(TimeoutError('bounded'),herdr('working'))],delay=0.08)
        hub=Hub(); service=create_service(hub,demo=True,agent_probe=probe)
        self.assertTrue(service.schedule_agent_refresh())
        self.assertFalse(service.schedule_agent_refresh())
        await asyncio.sleep(0.01)
        # Hub state operations remain responsive while the probe runs.
        self.assertEqual(hub.state_snapshot('a')['remote']['state'],'offline')
        await service.refresh_agent_now()
        self.assertEqual(probe.max_active,1)
        self.assertGreaterEqual(probe.calls,1)
        # A failed read-only probe lowers current readiness but keeps last-confirmed identity.
        await service.refresh_agent_now()
        state=service.state('a')
        self.assertEqual(state['agent']['status'],'unknown')
        self.assertEqual(state['agent']['default_agent']['actual_kind'],'codex')
        self.assertIsNotNone(state['agent']['last_confirmed'])

    async def test_non_demo_probe_is_configured_without_running_herdr(self):
        probe=SequenceProbe([(cap('unknown',running=False,exists=False,pane=False),herdr(running=False))])
        hub=Hub(); service=create_service(hub,demo=False,agent_probe=probe,
            default_menu=Path('src/omodachi_core/data/demo-menu.jsonc'),
            omodachi_menu=Path('src/omodachi_core/data/omodachi-menu.jsonc'),
            shell_config=Path('src/omodachi_core/data/demo-shell.json'))
        self.assertTrue(service.schedule_agent_refresh())
        await service.refresh_agent_now()
        self.assertFalse(service.state('a')['agent']['default_agent']['herdr_available'])
        self.assertFalse(service.state('a')['agent']['default_agent']['ready_to_attach'])


    async def test_probe_exception_degrades_current_state_and_emits_canonical_changes(self):
        class FailingProbe:
            def inspect(self):
                raise TimeoutError('probe timeout')
        hub=Hub(); service=create_service(hub,demo=True)
        before=service.state('a')
        self.assertTrue(before['agent']['default_agent']['ready_to_attach'])
        service.set_agent_probe(FailingProbe())
        cursor=hub.event_cursor
        self.assertFalse(await service.refresh_agent_now())
        after=service.state('a')
        current=after['agent']['default_agent']
        self.assertFalse(current['ready_to_attach'])
        self.assertFalse(current['default_agent_exists'])
        self.assertEqual(current['agent_status'],'unknown')
        self.assertEqual(current['default_agent_probe'],'unreadable')
        self.assertEqual(current['actual_kind'],'codex')
        self.assertIsNotNone(after['agent']['last_confirmed'])
        self.assertFalse(after['herdr']['available'])
        event_types=[event.type for event in hub.events_since(cursor)]
        self.assertIn('agent.changed',event_types)
        self.assertIn('catalog.changed',event_types)
        self.assertNotIn('agent.probe_failed',event_types)


if __name__ == '__main__': unittest.main()
