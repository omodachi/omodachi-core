// ODOMO-2: the Agent panel's store, as omodachi-ios `AgentChatStore` does it.
//
// Entering the panel is the ensure (A-63). Leaving it stops watching and
// nothing else - the provider's session is the host's and is never stopped,
// killed or recreated from here. A send is never retried on its own (the
// provider may already have it); a decision locks its card until the host
// answers.

import {useCallback, useEffect, useRef, useState} from 'react';
import * as A from './agent.js';
import {ApiError, newRequestId, openSocket} from './api.js';

function requestId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return newRequestId();
}

function codeOf(error) {
  if (error instanceof ApiError) return error.code === 'host_unreachable' ? 'host_unreachable' : A.safeCode(error.code);
  return 'agent_chat_unavailable';
}

// AGENT-FIX-2: the pause before the panel reconnects itself, by attempt:
// 0.5 s doubling to 8 s.
export function recoveryDelay(attempt) {
  return Math.min(8000, 500 * 2 ** Math.min(attempt, 4));
}

export function useAgent({client, base, WebSocketImpl, recoveryScale = 1}) {
  const [model, setModelState] = useState(A.initial);
  const ref = useRef(model);
  const setModel = useCallback((update) => {
    const next = typeof update === 'function' ? update(ref.current) : update;
    if (next && next !== ref.current) {
      ref.current = next;
      setModelState(next);
    }
  }, []);
  const visibility = useRef(0);
  const socket = useRef(null);
  const opening = useRef(false);
  const pending = useRef(null);
  // AGENT-FIX-2: reconnecting by itself (`recover`).
  const recoveryAttempt = useRef(0);
  const recovering = useRef(-1);
  const retryRef = useRef(null);

  const stopWatching = useCallback(() => {
    if (socket.current) socket.current.close();
    socket.current = null;
  }, []);

  const observe = useCallback((generation) => {
    const identity = ref.current.identity;
    if (!identity) return;
    stopWatching();
    socket.current = openSocket({client, base, path: '/v1/agent/default/chat/events', WebSocketImpl,
      onMessage: (data) => {
        if (generation !== visibility.current) return;
        let message;
        try {
          message = JSON.parse(data);
        } catch {
          return;
        }
        if (message && message.type === 'snapshot' && message.snapshot) {
          setModel((current) => A.restore(current, message.snapshot) || current);
          return;
        }
        // The host's queue overflowed: the only honest recovery is a re-read.
        if (message && message.type === 'resync_required') {
          setModel(A.connectionLost);
          stopWatching();
          recover(generation);
          return;
        }
        setModel((current) => A.apply(current, message));
        if (ref.current.phase === 'reconnecting') {
          stopWatching();
          recover(generation);
        }
      },
      onClose: () => {
        if (generation !== visibility.current) return;
        socket.current = null;
        setModel(A.connectionLost);
        recover(generation);
      }});
  }, [client, base, WebSocketImpl, setModel, stopWatching]); // eslint-disable-line react-hooks/exhaustive-deps

  // AGENT-FIX-2. The stream broke while the panel is open: read the host
  // again by itself (0.5 s, then 1, 2, 4, 8 s while it keeps failing). It used
  // to wait at "Reconnecting…" for a tap on Reconnect. Once per break.
  function recover(generation) {
    if (generation !== visibility.current || recovering.current === generation) return;
    recovering.current = generation;
    const delay = recoveryDelay(recoveryAttempt.current) * recoveryScale;
    recoveryAttempt.current += 1;
    setTimeout(async () => {
      const m = ref.current;
      if (generation !== visibility.current || socket.current || m.handoff.state !== 'none'
        || !(m.phase === 'reconnecting' || m.phase === 'failed')) return;
      await retryRef.current();
      const after = ref.current;
      if (after.phase === 'failed' && after.handoff.state === 'none' && recoveryAttempt.current < 6) {
        recover(visibility.current);
      }
    }, delay);
  }

  const open = useCallback(async () => {
    if (opening.current || socket.current) return;
    opening.current = true;
    visibility.current += 1;
    const generation = visibility.current;
    try {
      let snapshot;
      if (ref.current.identity) {
        try {
          snapshot = await client.chatSnapshot();
        } catch (error) {
          // AGENT-FIX-2: core is up but not attached to the agent (it
          // restarted, or lost its app-server): reading the snapshot again
          // can never fix that; the ensure entering the panel does.
          if (!(error instanceof ApiError) || error.code !== 'agent_chat_not_ready') throw error;
          snapshot = (await client.chatEnsure()).snapshot;
        }
      } else {
        const starting = A.beginEnsure(ref.current);
        if (!starting) return;
        setModel(starting);
        snapshot = (await client.chatEnsure()).snapshot;
      }
      if (generation !== visibility.current) return;
      // AGENT-NEW-1: the default conversation was replaced (here or on
      // another device): follow it, keeping only the words being typed.
      const base = A.otherConversation(ref.current, snapshot) ? A.startOver(ref.current) : ref.current;
      if (base !== ref.current) pending.current = null;
      const restored = A.restore(base, snapshot);
      if (!restored) throw new ApiError(409, 'agent_state_unknown');
      let next = {...restored, handoff: {state: 'none'}};
      recoveryAttempt.current = 0;
      if (pending.current && (snapshot.acceptedRequestIDs || []).includes(pending.current.id)) pending.current = null;
      setModel(next);
      observe(generation);
    } catch (error) {
      if (generation !== visibility.current) return;
      const code = codeOf(error);
      setModel((current) => ({...A.fail(current, {code}),
        handoff: code === 'existing_thread_handoff_required' ? {state: 'required'} : current.handoff}));
    } finally {
      opening.current = false;
    }
  }, [client, observe, setModel]);

  // AGENT-NEW-1: "New conversation". Asking shows the card; only its button
  // starts one, for the conversation on screen.
  const askNew = useCallback(() => setModel((m) => ({...m, fresh: {state: 'confirming'}})), [setModel]);
  const cancelNew = useCallback(() => setModel((m) => (m.fresh.state === 'starting' ? m : {...m, fresh: {state: 'none'}})), [setModel]);
  const confirmNew = useCallback(async () => {
    const current = ref.current;
    if (current.fresh.state !== 'confirming' || A.newRefusal(current)) return;
    const expected = current.identity.providerSessionID;
    setModel((m) => ({...m, fresh: {state: 'starting'}}));
    try {
      const result = await client.chatNew(expected);
      const snapshot = result && result.snapshot;
      const next = snapshot ? A.restore(A.startOver(ref.current), snapshot) : null;
      if (!next) throw new ApiError(409, 'agent_state_unknown');
      // Stop following the old conversation, then follow the new one.
      visibility.current += 1;
      const generation = visibility.current;
      stopWatching();
      pending.current = null;
      setModel(next);
      observe(generation);
    } catch (error) {
      const code = codeOf(error);
      if (code === 'agent_identity_changed') {
        // Another device was first: show the conversation that is now the default.
        setModel((m) => ({...m, fresh: {state: 'none'}}));
        visibility.current += 1;
        stopWatching();
        await open();
        return;
      }
      setModel((m) => ({...m, fresh: {state: 'problem', code}}));
    }
  }, [client, observe, open, setModel, stopWatching]);

  const leave = useCallback(() => {
    visibility.current += 1;
    stopWatching();
    setModel((current) => {
      let next = current;
      if (current.handoff.state !== 'none') next = {...next, handoff: {state: 'required'}};
      if (current.phase === 'starting' || current.phase === 'sending') next = A.connectionLost(next);
      return next;
    });
  }, [setModel, stopWatching]);

  const retry = useCallback(async () => {
    leave();
    await open();
  }, [leave, open]);
  retryRef.current = retry;

  const setDraft = useCallback((draft) => setModel((current) => ({...current, draft})), [setModel]);

  const notice = useCallback((key) => setModel((current) => ({...current, notice: key})), [setModel]);

  // ATTACH-1: `attachments` is `{refs, images, onSent}` from the chips: the
  // ids and markers ride with the words, and the chips go only once the host
  // took the message.
  // AGENT-PLAN-1 (Leo): what is typed goes to codex as typed - a slash
  // command too; the host handles the few it must (`A.sendAnswered`).
  const send = useCallback(async (attachments = null) => {
    const current = ref.current;
    const refs = attachments && attachments.refs ? attachments.refs : [];
    const begun = A.beginSend(current, refs.length ? {count: refs.length, images: attachments.images || 0} : null);
    if (!begun) return;
    const key = begun.text + '\u0000' + refs.map((r) => r.id).join(',');
    if (pending.current && pending.current.key !== key) {
      setModel(A.fail(current, {key: 'previousSendUnconfirmed'}));
      return;
    }
    const id = pending.current ? pending.current.id : requestId();
    pending.current = {id, text: begun.text, key};
    setModel(begun.model);
    try {
      const result = await client.chatSend(id, begun.text, current.selectedModel, current.selectedEffort, refs);
      pending.current = null;
      if (attachments && attachments.onSent) attachments.onSent();
      setModel((m) => ({...A.sendAnswered(m, result),
        notice: result && result.images === 'path' ? {key: 'pathOnly'} : m.notice}));
    } catch (error) {
      // The words go back into the composer; no automatic retry.
      const gone = error instanceof ApiError && error.code === 'attachment_gone';
      // A refusal the host names in words (Plan mode on a codex that cannot)
      // is said; anything else is "not confirmed".
      const code = error instanceof ApiError ? A.safeCode(error.code) : null;
      const said = ['agent_mode_unavailable', 'agent_busy'].includes(code) ? {code} : null;
      setModel((m) => A.sendFailed(m, begun.text, said || {key: gone ? 'attachmentGone' : 'sendUnconfirmed'}));
      if (gone) pending.current = null;
    }
  }, [client, setModel]);

  // AGENT-PLAN-1: codex's collaboration mode, changed in the thread itself.
  // The host answers with the usage the header reads (`usage.mode`); a turn
  // in flight refuses it, and the chip says why instead.
  const setMode = useCallback(async (mode) => {
    const refusal = A.modeRefusal(ref.current);
    if (refusal) {
      notice({key: refusal});
      return false;
    }
    setModel((m) => ({...m, modeBusy: true}));
    try {
      const result = await client.chatMode(mode);
      setModel((m) => ({...m, modeBusy: false, usage: A.usageOf(result && result.usage) || m.usage, notice: null}));
      return true;
    } catch (error) {
      const code = error instanceof ApiError ? A.safeCode(error.code) : null;
      setModel((m) => ({...m, modeBusy: false}));
      notice(code && code !== 'agent_chat_unavailable' ? {code} : {key: 'planFailed'});
      return false;
    }
  }, [client, notice, setModel]);

  // After a plan: what codex's TUI offers as "Yes, implement this plan" -
  // back to the default mode, then the words that ask for it, as a message of
  // the person's own. What they were typing stays in the composer.
  const implementPlan = useCallback(async (words) => {
    if (!A.actionablePlan(ref.current)) return;
    if (!(await setMode('default'))) return;
    const current = ref.current;
    const kept = current.draft;
    const begun = A.beginSend({...current, draft: words});
    if (!begun) return;
    const id = requestId();
    setModel({...begun.model, draft: kept});
    try {
      await client.chatSend(id, begun.text, current.selectedModel, current.selectedEffort, []);
      setModel((m) => A.sendAcknowledged(m));
    } catch {
      setModel((m) => ({...A.sendFailed(m, begun.text, {key: 'sendUnconfirmed'}), draft: m.draft || kept}));
    }
  }, [client, setMode, setModel]);

  const steerRef = useRef(null);
  const steer = useCallback(async () => {
    const current = ref.current;
    if (!A.canSteer(current)) return;
    const text = current.draft;
    try {
      await client.chatSteer(requestId(), text);
      setModel((m) => (m.draft === text ? {...m, draft: ''} : m));
    } catch (error) {
      const code = error instanceof ApiError ? error.code : null;
      notice(code === 'agent_command_while_working' ? {code} : {key: code === 'agent_not_working' ? 'steerTooLate' : 'steerUnconfirmed'});
    }
  }, [client, notice, setModel]);
  steerRef.current = steer;

  // AGENT-QUEUE-1: Send while a turn runs. codex keeps the words in its own
  // queue and starts them when the turn ends; the composer empties once the
  // host has them, and the queue row above it shows them meanwhile.
  const queue = useCallback(async () => {
    const current = ref.current;
    if (!A.canQueue(current)) return;
    const text = current.draft;
    try {
      const result = await client.chatQueue(requestId(), text);
      setModel((m) => ({...(m.draft === text ? {...m, draft: ''} : m), queue: A.queueOf(result && result.queue)}));
    } catch (error) {
      const code = error instanceof ApiError ? A.safeCode(error.code) : null;
      // A codex without the queue after all: the words are steered in, as before.
      if (code === 'agent_queue_unavailable') {
        setModel((m) => ({...m, queue: null}));
        await steerRef.current();
        return;
      }
      notice(code === 'agent_command_while_working' ? {code} : {key: 'queueUnconfirmed'});
    }
  }, [client, notice, setModel]);

  // A waiting message: Send now (into the running turn, or started between
  // turns), Edit (new words in its place), Delete. An edit that finds it gone
  // (it started meanwhile) is not lost: the words go back into the composer.
  const queueItem = useCallback(async (id, action, text = null) => {
    try {
      const result = await client.chatQueueItem(id, action, text);
      setModel((m) => ({...m, queue: A.queueOf(result && result.queue)}));
      return true;
    } catch (error) {
      const code = error instanceof ApiError ? A.safeCode(error.code) : null;
      if (code === 'agent_queue_item_gone') {
        setModel((m) => ({...m, queue: (m.queue || []).filter((item) => item.id !== id),
          draft: action === 'edit' && text && !m.draft.trim() ? text : m.draft,
          notice: action === 'edit' ? {key: 'queueEditGone'} : m.notice}));
        return action !== 'edit';
      }
      notice({key: 'queueUnconfirmed'});
      return false;
    }
  }, [client, notice, setModel]);

  // AGENT-QUEUE-1: "Earlier messages" - the rows before the first one here.
  const loadEarlier = useCallback(async () => {
    const current = ref.current;
    if (!current.earlier || current.loadingEarlier || !current.rows.length) return;
    const first = current.rows[0];
    setModel((m) => ({...m, loadingEarlier: true}));
    try {
      const page = await client.chatHistory(first.itemID, 100);
      setModel((m) => (m.rows.length && m.rows[0].id === first.id ? A.prependEarlier(m, page) : {...m, loadingEarlier: false}));
    } catch (error) {
      setModel((m) => ({...m, loadingEarlier: false}));
      // The conversation is not the one these rows came from: read it again.
      if (error instanceof ApiError && error.code === 'agent_history_row_unknown') await retryRef.current();
    }
  }, [client, setModel]);

  const cancel = useCallback(async () => {
    const current = ref.current;
    if (!A.canCancel(current)) return;
    const turn = current.turnID;
    setModel({...current, phase: 'cancelling'});
    try {
      await client.chatInterrupt(turn);
    } catch {
      setModel((m) => A.fail(m, {key: 'stopUnconfirmed'}));
    }
  }, [client, setModel]);

  const resolve = useCallback(async (id, answer) => {
    try {
      const result = await client.chatResolve(id, answer);
      if (!result || result.resolved !== true) {
        setModel((m) => ({...A.failDecision(m, id), notice: {key: 'answerUnconfirmed'}}));
        return;
      }
      setModel((m) => {
        const next = A.settleDecision(m, id, result.decision || answer.decision || 'input', 'client');
        // AGENT-QUEUE-1: a reason neither the turn nor the queue would take
        // stays with the person, in the composer.
        return result.delivery === 'unsent' && answer.reason && !next.draft.trim() ? {...next, draft: answer.reason} : next;
      });
    } catch (error) {
      if (error instanceof ApiError && error.code === 'agent_approval_unknown') {
        setModel((m) => A.settleDecision(m, id, null, 'elsewhere'));
        return;
      }
      setModel((m) => ({...A.failDecision(m, id), notice: {key: 'answerUnconfirmed'}}));
    }
  }, [client, setModel]);

  const decide = useCallback((id, decision) => {
    const next = A.beginDecision(ref.current, id, decision);
    if (!next) return;
    setModel(next);
    resolve(id, {decision});
  }, [resolve, setModel]);

  // AGENT-QUEUE-1: "No, and here's why" - a decline with one line of words.
  const declineWithReason = useCallback((id, reason) => {
    const words = typeof reason === 'string' ? reason.trim() : '';
    const next = A.beginDecision(ref.current, id, 'decline');
    if (!next) return;
    setModel(next);
    resolve(id, words ? {decision: 'decline', reason: words} : {decision: 'decline'});
  }, [resolve, setModel]);

  const answer = useCallback((id, answers) => {
    const input = A.submission(answers);
    if (!Object.keys(input).length) return;
    const next = A.beginAnswer(ref.current, id, input);
    if (!next) return;
    setModel(next);
    resolve(id, {input});
  }, [resolve, setModel]);

  const loadModels = useCallback(async () => {
    if (ref.current.modelsLoading || !ref.current.identity) return;
    setModel((m) => ({...m, modelsLoading: true}));
    try {
      const models = await client.chatModels();
      setModel((m) => ({...m, models, modelsLoading: false, modelsNotice: null}));
    } catch {
      setModel((m) => ({...m, modelsLoading: false, modelsNotice: 'modelsUnreadable'}));
    }
  }, [client, setModel]);

  const choose = useCallback((modelId, effort) => setModel((m) => ({...m, selectedModel: modelId, selectedEffort: effort})), [setModel]);

  const prepareHandoff = useCallback(async () => {
    if (!['required', 'problem'].includes(ref.current.handoff.state)) return;
    const generation = visibility.current;
    setModel((m) => ({...m, handoff: {state: 'preparing'}}));
    try {
      const plan = await client.chatHandoffPrepare();
      if (generation !== visibility.current) return;
      if (!A.planUsable(plan)) throw new ApiError(409, 'handoff_target_changed');
      setModel((m) => ({...m, handoff: {state: 'prepared', plan}}));
    } catch (error) {
      if (generation !== visibility.current) return;
      setModel((m) => ({...m, handoff: {state: 'problem', code: codeOf(error)}}));
    }
  }, [client, setModel]);

  // Wired ONLY to the explicit confirmation once the plan is on screen.
  const confirmHandoff = useCallback(async () => {
    const handoff = ref.current.handoff;
    if (handoff.state !== 'prepared') return;
    const plan = handoff.plan;
    const generation = visibility.current;
    setModel((m) => ({...m, handoff: {state: 'confirming', plan}}));
    try {
      const result = await client.chatHandoffConfirm(plan.plan_id);
      if (generation !== visibility.current) return;
      if (!A.handoffConfirmed(plan, result)) throw new ApiError(409, 'handoff_thread_unconfirmed');
      setModel((m) => ({...m, handoff: {state: 'none'}}));
      await retry();
    } catch (error) {
      if (generation !== visibility.current) return;
      setModel((m) => ({...m, handoff: {state: 'problem', code: codeOf(error)}}));
    }
  }, [client, retry, setModel]);

  const deferHandoff = useCallback(() => setModel((m) => (m.handoff.state === 'confirming' ? m
    : {...m, handoff: {state: 'required'}})), [setModel]);


  // The panel on screen: open; off screen: stop watching. Coming back to the
  // page (a phone unlocked, the network back) reads the host again.
  useEffect(() => {
    open();
    const wake = () => {
      if (document.visibilityState !== 'visible') return;
      if (ref.current.phase === 'reconnecting' || (ref.current.phase === 'failed' && !ref.current.identity)) retry();
    };
    document.addEventListener('visibilitychange', wake);
    window.addEventListener('online', wake);
    return () => {
      document.removeEventListener('visibilitychange', wake);
      window.removeEventListener('online', wake);
      leave();
    };
  }, [open, leave, retry]);

  const getDraft = useCallback(() => ref.current.draft, []);

  return {model, setDraft, getDraft, send, steer, cancel, decide, answer, retry,
    dismissNotice: () => notice(null), loadModels, choose, prepareHandoff, confirmHandoff, deferHandoff,
    askNew, cancelNew, confirmNew, setMode, implementPlan, queue, queueItem, loadEarlier, declineWithReason,
    closeDiff: () => setModel((m) => ({...m, diff: null}))};
}
