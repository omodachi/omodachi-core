// AGENT-PLAN-1: Odomo's /plan and Plan chip, on core's fixtures generated
// from real codex 0.160 frames: the thread's mode as codex says it, the plan as
// its own card, the way back to the default mode, and "Implement this plan".
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import * as A from '../src/lib/agent.js';
import {HostClient} from '../src/lib/api.js';
import {en, zh} from '../src/lib/i18n.js';
import {agentErrorText} from '../src/lib/reasons.js';

const FIXTURES = new URL('../../contracts/fixtures/', import.meta.url);
const fixture = (name) => JSON.parse(readFileSync(new URL(name, FIXTURES), 'utf8'));
const render = async () => {
  const {runnerImport} = await import('vite');
  const {module} = await runnerImport(new URL('./render.jsx', import.meta.url).pathname,
    {configFile: false, logLevel: 'silent'});
  return module;
};

test('the captured Plan turn: mode from codex, the plan as a marked row after its introduction', () => {
  const m = A.restore(A.initial(), fixture('agent-chat-snapshot-plan.json'));
  assert.equal(m.usage.mode, 'plan');
  assert.ok(A.planOn(m));
  const plan = m.rows.find((row) => row.plan);
  assert.ok(plan, 'the plan row');
  assert.equal(plan.role, 'assistant');
  assert.match(plan.text, /^# Read the port from the environment/);
  assert.doesNotMatch(plan.text, /proposed_plan/);
  const turn = m.rows.filter((row) => row.turnID === plan.turnID).map((row) => (row.plan ? 'plan' : row.role));
  assert.deepEqual(turn, ['user', 'assistant', 'plan']);
  // the newest plan, Plan mode on, nothing said after it: the person can act on it
  assert.equal(A.actionablePlan(m), plan.id);
  assert.equal(A.actionablePlan({...m, usage: {...m.usage, mode: 'default'}}), null);
  const later = {...m, rows: m.rows.concat({kind: 'message', id: 'message:u9', itemID: 'u9', turnID: 't9', role: 'user', text: 'more'})};
  assert.equal(A.actionablePlan(later), null);
  // a usage without a mode (an older core) is no mode at all
  assert.equal(A.usageOf({model: 'x', effort: 'y'}).mode, null);
  assert.equal(A.usageOf({model: 'x', effort: 'y', mode: 'auto'}).mode, null);
});

test('the plan streams in as a message event and the mode follows agent.usage.updated', () => {
  const event = fixture('agent-chat-event-plan.json');
  assert.equal(event.event.value.plan, true);
  let m = A.restore(A.initial(), {...fixture('agent-chat-snapshot-plan.json'), activeTurn: event.event.value.turnID,
    sequence: 0, identity: event.identity, rows: []});
  m = A.apply(m, event);
  assert.equal(m.rows.length, 1);
  assert.equal(m.rows[0].plan, true);
  m = A.apply(m, {identity: event.identity, sequence: 2, event: {type: 'agent.usage.updated',
    usage: {tokens: null, rate_limits: null, model: 'gpt-fixture', effort: 'high', mode: 'default'}}});
  assert.equal(m.usage.mode, 'default');
  assert.ok(!A.planOn(m));
});

test('the mode changes between turns only, and says why otherwise', () => {
  const m = A.restore(A.initial(), fixture('agent-chat-snapshot-plan.json'));
  assert.equal(A.modeRefusal(m), null);
  assert.equal(A.modeRefusal({...m, phase: 'running', turnID: 't'}), 'planWhileWorking');
  assert.equal(A.modeRefusal({...m, modeBusy: true}), 'planSwitching');
  assert.equal(A.modeRefusal(A.initial()), 'newUnavailable');
  assert.equal(en.agent.planWhileWorking, 'Plan mode can change once this turn has finished.');
  assert.equal(zh.agent.planWhileWorking, '这一轮跑完后，才能切换计划模式。');
});

test('the chip asks the host for the mode, and the answer is the usage it shows', async () => {
  const calls = [];
  const client = new HostClient({base: 'https://h.example/', token: 't', fetchImpl: async (url, init) => {
    calls.push({path: new URL(url).pathname, method: init.method, body: JSON.parse(init.body)});
    return {ok: true, status: 200, json: async () => fixture('agent-chat-mode.json')};
  }});
  const result = await client.chatMode('plan');
  assert.deepEqual(calls, [{path: '/v1/agent/default/chat/mode', method: 'POST', body: {mode: 'plan'}}]);
  assert.equal(A.usageOf(result.usage).mode, 'plan');
});

test('what is typed goes through; the host answers the four basics itself', async () => {
  // A slash command is a message like any other (no list, no hint, no catalog).
  const calls = [];
  const client = new HostClient({base: 'https://h.example/', token: 't', fetchImpl: async (url, init) => {
    calls.push({path: new URL(url).pathname, body: JSON.parse(init.body)});
    return {ok: true, status: 200, json: async () => ({accepted: true, delivery: 'command', command: 'plan', mode: 'plan'})};
  }});
  const reply = await client.chatSend('r1', '/plan', null, null);
  assert.deepEqual(calls, [{path: '/v1/agent/default/chat/messages', body: {request_id: 'r1', text: '/plan'}}]);
  // `/plan` alone starts no turn: the words leave the screen, nothing waits.
  const ready = A.restore(A.initial(), fixture('agent-chat-snapshot-plan.json'));
  const begun = A.beginSend({...ready, draft: '/plan'});
  assert.equal(begun.model.phase, 'sending');
  const after = A.sendAnswered(begun.model, reply);
  assert.equal(after.phase, 'ready');
  assert.equal(after.localEcho, null);
  // `/plan <words>` and `/review` are turns whose user row is other words than typed.
  const turn = A.sendAnswered(begun.model, {accepted: true, turnID: 't9', delivery: 'accepted', command: 'plan', mode: 'plan'});
  assert.equal(turn.phase, 'sending');
  assert.equal(turn.localEcho.attached, true, 'any user row of the turn is the echo');
  const plain = A.sendAnswered(begun.model, {accepted: true, turnID: 't9', delivery: 'accepted'});
  assert.ok(!plain.localEcho.attached);
  // `/diff` brings the conversation's change cards.
  const diff = A.sendAnswered(begun.model, {accepted: true, delivery: 'command', command: 'diff', changes_omitted: 3,
    changes: [{path: '/w/app.py', kind: 'update', added: 1, removed: 1, diff: '@@ -1 +1 @@\n-a\n+b', turnID: 't1'}]});
  assert.equal(diff.phase, 'ready');
  assert.deepEqual(diff.diff.changes.map((c) => [c.path, c.added, c.removed]), [['/w/app.py', 1, 1]]);
  assert.equal(diff.diff.omitted, 3);
  assert.deepEqual(A.sendAnswered(begun.model, {delivery: 'command', command: 'diff'}).diff, {changes: [], omitted: 0});
  // the refusals are sentences, both languages
  for (const code of ['agent_mode_unavailable', 'invalid_agent_mode', 'agent_command_while_working']) {
    assert.equal(A.safeCode(code), code);
    for (const t of [en, zh]) assert.doesNotMatch(agentErrorText(t, {code}), /_/);
  }
  // nothing of the slash list is left
  for (const name of ['parseSlash', 'slashMatches', 'slashExact', 'selectSlash', 'slashRefusal', 'commandText']) {
    assert.equal(A[name], undefined, name);
  }
  for (const t of [en, zh]) {
    for (const key of ['cmd', 'commandReasons', 'hint', 'slashButton', 'run', 'literalText', 'unknownCommand']) {
      assert.equal(t.agent[key], undefined, key);
    }
  }
});

test('the changes card draws codex\'s own change cards, or says there are none', async () => {
  const r = await render();
  const html = r.renderDiffCard({diff: {changes: [{path: '/w/app.py', kind: 'update', movePath: null, added: 1, removed: 1,
    diff: ['@@ -1 +1 @@', '-port = 8080', '+port = 9090'], diffOmitted: 0}], omitted: 2}});
  assert.match(html, /data-testid="agent-diff"/);
  assert.match(html, /Changes in this conversation/);
  assert.match(html, /data-testid="agent-change-diff-0"/);
  assert.match(html, /port = 9090/);
  assert.match(html, /2 earlier changes are not shown\./);
  assert.match(html, /data-testid="agent-diff-close"/);
  const none = r.renderDiffCard({diff: {changes: [], omitted: 0}}, 'zh-Hans');
  assert.match(none, /这段对话还没有改过文件。/);
});

test('the chip and the plan card draw, in both languages', async () => {
  const r = await render();
  const chip = r.renderPlanChip({refusal: null});
  assert.match(chip, /data-testid="agent-plan-chip"/);
  assert.match(chip, /aria-label="Plan mode is on"/);
  assert.match(chip, /Tap to go back to the default mode\./);
  assert.match(chip, />Plan</);
  assert.match(r.renderPlanChip({refusal: zh.agent.planWhileWorking}, 'zh-Hans'), /这一轮跑完后/);
  const m = A.restore(A.initial(), fixture('agent-chat-snapshot-plan.json'));
  const row = m.rows.find((x) => x.plan);
  const card = r.renderPlanCard({row, plan: {refusal: null, onImplement: () => {}}});
  assert.match(card, /data-testid="agent-plan-[^"]+-plan"/);
  assert.match(card, /Proposed plan/);
  assert.match(card, /<h1[^>]*>Read the port from the environment<\/h1>|Read the port from the environment/);
  assert.match(card, /data-testid="agent-plan-implement"[^>]*>Implement this plan</);
  assert.match(card, /This leaves Plan mode and asks the agent to start\./);
  // AGENT-QUEUE-1: "Keep planning" took the old "or keep writing" line's place.
  assert.match(card, /data-testid="agent-plan-keep"[^>]*>Keep planning</);
  const zhCard = r.renderPlanCard({row, plan: {refusal: null, onImplement: () => {}}}, 'zh-Hans');
  assert.match(zhCard, /拟定的计划/);
  assert.match(zhCard, /按这个计划动手/);
  // an older plan, or Plan mode off: the card without the question
  assert.doesNotMatch(r.renderPlanCard({row}), /agent-plan-implement/);
  // the words "Implement this plan" sends, as codex's TUI does (English) and in Chinese
  assert.equal(en.agent.planImplementMessage, 'Implement the plan.');
  assert.ok(zh.agent.planImplementMessage);
});

test('the panel wires the chip, the plan card and the changes card - and no slash list', () => {
  const panel = readFileSync(new URL('../src/panels/AgentPanel.jsx', import.meta.url), 'utf8');
  assert.match(panel, /planOn \? \(\s*<PlanChip/);
  assert.match(panel, /store\.setMode\('default'\)/);
  assert.match(panel, /store\.implementPlan\(t\.agent\.planImplementMessage\)/);
  assert.match(panel, /model\.diff \? <DiffCard/);
  for (const gone of [/agent-slash/, /agent-run-command/, /OnceHint/, /parseSlash/, /loadCommands/]) {
    assert.doesNotMatch(panel, gone);
  }
  const icon = readFileSync(new URL('../src/Icon.jsx', import.meta.url), 'utf8');
  assert.match(icon, /\n {2}plan: 'M/);
});
