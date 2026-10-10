import React, {useEffect, useLayoutEffect, useRef, useState} from 'react';
import Icon from '../Icon.jsx';
import AgentFace from '../AgentFace.jsx';
import {faceWord} from '../lib/agentFace.js';
import {useCopy} from '../copy.js';
import {fill} from '../lib/i18n.js';
import * as A from '../lib/agent.js';
import {useAgent} from '../lib/useAgent.js';
import {appLinks} from '../lib/applink.js';
import {agentErrorText, agentReason} from '../lib/reasons.js';
import {useAttachments} from '../lib/useAttachments.js';
import {AttachButton, AttachmentChips, AttachNotice, DropVeil, useDropTarget} from './Attachments.jsx';
import Markdown, {marked} from '../Markdown.jsx';
import * as S from '../lib/agentSteps.js';
import LinkNotice from '../LinkNotice.jsx';
import {useHostOpen} from '../lib/useHostOpen.js';
import {useFittingPlaceholder} from '../lib/placeholder.js';

// ODOMO-2: panel ③, Agent - the App's (omodachi-ios herdr-4
// `Surfaces/AgentPanel/AgentPanelView.swift`, `AgentApprovalCard.swift`).
//
// No bubbles, no avatars, no timestamps: a speaker label and a 2px accent
// rule down the user's own words. An approval sits in the stream with
// exactly the decisions the provider offered, and after it is answered it is
// one result line. The composer sends on Enter where there is a real
// keyboard (Shift+Enter is a new line); on a phone Enter is a new line and
// the Send button sends - dictation is the phone keyboard's own.

// A pointer that hovers is a desktop: files can be dropped on the panel.
function enterSendsDefault() {
  return wantsEnterToSend();
}

function wantsEnterToSend() {
  try {
    return typeof matchMedia === 'function' && matchMedia('(hover: hover) and (pointer: fine)').matches;
  } catch {
    return false;
  }
}

function phaseLabel(phase, t) {
  return {dormant: '—', starting: t.agent.phaseStarting, ready: t.agent.phaseReady, sending: t.agent.phaseSending,
    running: t.agent.phaseWorking, cancelling: t.agent.phaseStopping, reconnecting: t.agent.phaseReconnecting,
    failed: t.agent.phaseFailed}[phase] || '—';
}

function statusLine(model, t) {
  const parts = [phaseLabel(model.phase, t)];
  if (model.status.waiting === 'approval') parts.push(t.agent.waitingOnYou);
  else if (model.status.waiting === 'user_input') parts.push(t.agent.needsInput);
  else if (model.status.waiting) parts.push(model.status.type || '—');
  const waiting = A.pendingApprovals(model).length;
  if (waiting) parts.push(fill(t.agent.waitingCount, waiting));
  return parts.join(' · ');
}

function kindTitle(kind, t) {
  return {commandExecution: t.agent.kindCommand, fileChange: t.agent.kindFileChange,
    permissions: t.agent.kindPermissions, userInput: t.agent.kindUserInput}[kind] || kind;
}

function decisionLabel(decision, t) {
  // A `userInput` prompt is answered with values, not a decision.
  return {accept: t.agent.accept, acceptForSession: t.agent.acceptSession, decline: t.agent.decline,
    cancel: t.agent.cancel, input: t.agent.answered}[decision] || decision;
}

function Usage({usage, t}) {
  if (!usage) return null;
  const meters = [];
  const fraction = A.contextFraction(usage);
  if (fraction !== null) meters.push([t.agent.context, fraction]);
  for (const window of usage.windows) {
    if (window.usedPercent !== null) meters.push([A.windowLabel(window.minutes), Math.min(1, Math.max(0, window.usedPercent / 100))]);
  }
  if (!meters.length) return null;
  return (
    <div className="ausage" data-testid="agent-usage">
      {meters.map(([label, value]) => (
        <span key={label} className="ausage__m" aria-label={label + ': ' + Math.round(value * 100) + '%'}>
          <span>{label + ': ' + Math.round(value * 100) + '%'}</span>
          <i><b className={value > 0.9 ? 'warn' : ''} style={{width: Math.round(44 * value) + 'px'}} /></i>
        </span>
      ))}
    </div>
  );
}

// LINKS-1: the agent's words are markdown (headings, lists, code blocks,
// links); the person's own words stay exactly as typed.
function Message({row, streaming, t, onLink, mark = '', plan = null}) {
  const you = row.role === 'user';
  const cursor = streaming ? <i className="acursor" aria-hidden="true" /> : null;
  if (row.plan) return <PlanCard row={row} cursor={cursor} t={t} onLink={onLink} mark={mark} plan={plan} />;
  return (
    <div className={'amsg' + (you ? ' amsg--you' : '')} data-testid={'agent-message-' + row.itemID}>
      <span className="amsg__who">{you ? t.agent.roleYou : t.agent.roleAgent}</span>
      {you || !onLink ? <p className="amsg__tx">{marked(row.text, mark)}{cursor}</p> : (
        <div className="amsg__tx amsg__md">
          <Markdown text={row.text} mark={mark} onLink={(link) => onLink({kind: 'agent', item: row.itemID}, link)}>{cursor}</Markdown>
        </div>
      )}
      {row.textTruncated ? (
        <p className="atool__cut" data-testid={'agent-message-trimmed-' + row.itemID}>{fill(t.agent.trimmed, row.textTruncated)}</p>
      ) : null}
      {row.images ? (
        <span className="amsg__pics" data-testid={'agent-message-pictures-' + row.itemID}>
          <Icon name="image" size={14} />{row.images === 1 ? t.attach.picture : fill(t.attach.pictures, row.images)}
        </span>
      ) : null}
    </div>
  );
}

// AGENT-PLAN-1: the plan codex proposed in Plan mode, as a card of its own -
// a title, the plan's markdown, and on the newest one while Plan mode is on,
// what codex's TUI asks next: implement it (back to the default mode and
// "Implement the plan.") or keep writing to refine it.
export function PlanCard({row, cursor = null, t, onLink, mark = '', plan = null}) {
  return (
    <div className="aplan" data-testid={'agent-plan-' + row.itemID} role="group" aria-label={t.agent.planTitle}>
      <span className="aplan__t"><Icon name="plan" size={14} />{t.agent.planTitle}</span>
      <div className="amsg__tx amsg__md">
        {onLink ? (
          <Markdown text={row.text} mark={mark} onLink={(link) => onLink({kind: 'agent', item: row.itemID}, link)}>{cursor}</Markdown>
        ) : <p>{marked(row.text, mark)}{cursor}</p>}
      </div>
      {plan ? (
        <div className="aplan__act">
          <button type="button" className="fbtn fbtn--primary" data-testid="agent-plan-implement"
            disabled={plan.refusal ? true : undefined} title={plan.refusal || undefined}
            onClick={plan.onImplement}>{t.agent.planImplement}</button>
          {/* AGENT-QUEUE-1: or stay in Plan mode and say what to change -
              the composer takes the words. */}
          <button type="button" className="fbtn" data-testid="agent-plan-keep" title={t.agent.planKeepHint}
            onClick={plan.onKeep}>{t.agent.planKeep}</button>
          <p className="dim">{plan.refusal || t.agent.planImplementHint}</p>
        </div>
      ) : null}
    </div>
  );
}

// AGENT-PLAN-1: Plan mode on - a small chip in the header. A tap goes back to
// the default mode; while a turn runs it says why it cannot (the App's A-65:
// never a dimmed control that looks broken).
export function PlanChip({refusal, t, onLeave}) {
  const hint = refusal || t.agent.planChipHint;
  return (
    <button type="button" className="fbtn apanel__plan" data-testid="agent-plan-chip" aria-label={t.agent.planChipLabel}
      title={hint} aria-description={hint} onClick={onLeave}>
      <Icon name="plan" size={14} /><span>{t.agent.planChip}</span>
    </button>
  );
}

// AGENT-FIX-2: "64 KB", "1.2 MB" - characters counted as bytes.
function sizeText(characters) {
  if (characters < 1024) return characters + ' B';
  if (characters < 1024 * 1024) return Math.round(characters / 1024) + ' KB';
  return (characters / (1024 * 1024)).toFixed(1) + ' MB';
}

const GLYPH = {running: '…', succeeded: '✓', failed: '!', cancelled: '–', interrupted: '–', unknown: '?'};

// AGENT-COLLIE-1: a step in a few words - a verb in the chrome face and the
// thing it acted on - with find's marks.
function StepTitle({row, t, mark}) {
  const line = S.stepLine(t, row);
  return (
    <span className="atool__n">
      {line.verb ? <span className="atool__v">{line.verb + ' '}</span> : null}
      <span className={line.mono ? 'atool__m' : ''}>{marked(line.subject, mark)}</span>
    </span>
  );
}

function statusText(row, t) {
  const word = t.agent.toolStatus[row.status] || row.status;
  return row.status === 'failed' && Number.isInteger(row.exitCode) ? word + ' · ' + fill(t.agent.toolExit, row.exitCode) : word;
}

// AGENT-COLLIE-1: one changed file, as codex sent it - "Edited app.py +3 −2",
// the first lines of its diff and a fold for the rest. Only the change codex
// itself put in its item: this is not a file viewer, and the path opens on
// the computer.
function ChangeCard({row, change, index, t, onLink, waiting, mark}) {
  const [all, setAll] = useState(false);
  const lines = S.diffLines(change);
  const shown = all ? lines : lines.slice(0, S.DIFF_PREVIEW);
  const verb = S.changeVerb(t, change);
  return (
    <div className={'achg' + (waiting ? ' achg--wait' : '')} data-testid={'agent-change-' + row.itemID + '-' + index}>
      <div className="achg__h">
        <span className={'atool__g atool__g--' + row.status} aria-hidden="true">{GLYPH[row.status]}</span>
        <span className="atool__v">{marked(verb, mark)}</span>
        {onLink ? (
          <button type="button" className="md__file achg__f" title={change.path} data-testid="agent-tool-path"
            onClick={() => onLink({kind: 'agent', item: row.itemID}, {kind: 'path', target: change.path})}>
            {marked(S.fileName(change.path), mark)}
          </button>
        ) : <span className="achg__f">{marked(S.fileName(change.path), mark)}</span>}
        {change.movePath ? <span className="achg__f">{'→ '}{marked(S.moveName(change), mark)}</span> : null}
        <span className="grow" />
        <span className="achg__n" aria-label={fill(t.agent.changeLinesLabel, change.added, change.removed)}>
          {change.added > 0 || change.removed === 0 ? <span className="achg__add">{'+' + change.added}</span> : null}
          {change.added > 0 && change.removed > 0 ? ' ' : null}
          {change.removed > 0 ? <span className="achg__del">{'−' + change.removed}</span> : null}
        </span>
      </div>
      {waiting ? <p className="achg__w" data-testid={'agent-change-waiting-' + row.itemID}>{t.agent.changeWaiting}</p> : null}
      {shown.length ? (
        <div className="achg__d" role="group" aria-label={verb + ' ' + S.changeSubject(change)}>
          {shown.map((line, i) => (line.kind === 'hunk'
            ? <div key={i} className="achg__sep" aria-hidden="true">⋯</div>
            : (
              <div key={i} className={'achg__l achg__l--' + line.kind}>
                <span className="achg__sg" aria-hidden="true">{line.kind === 'add' ? '+' : line.kind === 'del' ? '−' : ' '}</span>
                <span className="achg__t">{line.text || ' '}</span>
              </div>
            )))}
        </div>
      ) : null}
      {lines.length > S.DIFF_PREVIEW ? (
        <button type="button" className="achg__more" aria-expanded={all} data-testid={'agent-change-more-' + row.itemID + '-' + index}
          onClick={() => setAll(!all)}>
          {all ? t.agent.changeLess : fill(t.agent.changeMore, lines.length - S.DIFF_PREVIEW)}
        </button>
      ) : null}
      {change.diffOmitted ? <p className="atool__cut">{fill(t.agent.changeOmitted, change.diffOmitted)}</p> : null}
    </div>
  );
}

function ChangeCards({row, t, onLink, waiting, mark}) {
  return (
    <div className="achgs" data-testid={'agent-tool-' + row.itemID}>
      {row.changes.map((change, index) => (
        <ChangeCard key={change.path + index} row={row} change={change} index={index} t={t} onLink={onLink}
          waiting={waiting} mark={mark} />
      ))}
      {row.changesOmitted ? <p className="atool__cut">{fill(t.agent.changeFilesOmitted, row.changesOmitted)}</p> : null}
    </div>
  );
}

// `compact`: a step inside an open run - an edit is one line until tapped,
// unless a prompt is waiting on it.
export function ToolRow({row, t, onLink, compact = false, waiting = false, mark = ''}) {
  const [open, setOpen] = useState(false);
  if (row.changes && (!compact || waiting)) {
    return <ChangeCards row={row} t={t} onLink={onLink} waiting={waiting} mark={mark} />;
  }
  if (row.changes) {
    const added = row.changes.reduce((n, c) => n + c.added, 0);
    const removed = row.changes.reduce((n, c) => n + c.removed, 0);
    return (
      <div className="atool" data-testid={'agent-tool-' + row.itemID}>
        <button type="button" className="atool__row" aria-expanded={open} onClick={() => setOpen(!open)}>
          <span className={'atool__g atool__g--' + row.status} aria-hidden="true">{GLYPH[row.status]}</span>
          <StepTitle row={row} t={t} mark={mark} />
          <span className="grow" />
          <span className="achg__n" aria-label={fill(t.agent.changeLinesLabel, added, removed)}>
            <span className="achg__add">{'+' + added}</span> <span className="achg__del">{'−' + removed}</span>
          </span>
          <Icon name={open ? 'down' : 'right'} size={12} />
        </button>
        {open ? <ChangeCards row={row} t={t} onLink={onLink} waiting={false} mark={mark} /> : null}
      </div>
    );
  }
  const glyph = GLYPH[row.status];
  return (
    <div className="atool" data-testid={'agent-tool-' + row.itemID}>
      <button type="button" className="atool__row" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className={'atool__g atool__g--' + row.status} aria-hidden="true">{glyph}</span>
        <StepTitle row={row} t={t} mark={mark} />
        <span className="grow" />
        <span className="atool__s">{statusText(row, t)}</span>
        <Icon name={open ? 'down' : 'right'} size={12} />
      </button>
      {/* LINKS-1: the files it touched or looked at, to open on the computer. */}
      {row.paths && row.paths.length && onLink ? (
        <div className="atool__paths">
          {row.paths.map((path) => (
            <button key={path} type="button" className="md__file" title={path} data-testid="agent-tool-path"
              onClick={() => onLink({kind: 'agent', item: row.itemID}, {kind: 'path', target: path})}>
              {path.split('/').filter(Boolean).pop() || path}
            </button>
          ))}
        </div>
      ) : null}
      {open && row.detail && row.detailTruncated ? (
        <p className="atool__cut" data-testid={'agent-tool-truncated-' + row.itemID}>
          {fill(t.agent.toolTruncated, sizeText(row.detailTruncated))}
        </p>
      ) : null}
      {open && row.detail ? <pre className="atool__d">{row.detail}</pre> : null}
    </div>
  );
}

const OTHER = '\u0000other';

// AGENT-COLLIE-1: what one question is answered with - the chosen label, or
// the person's own words for "Other…" (and for a question with no options).
function valueOf(question, picked, typed) {
  const choice = picked[question.id];
  if (choice === OTHER || !question.choices.length) return typed[question.id] || '';
  return choice || '';
}

function QuestionBody({question, picked, typed, setPicked, setTyped, t}) {
  const choice = picked[question.id];
  const field = (
    <input className="acard__in" type={question.secret ? 'password' : 'text'}
      placeholder={question.choices.length ? t.agent.questionOtherPlaceholder : t.agent.yourAnswer}
      aria-label={question.choices.length ? t.agent.questionOtherPlaceholder : question.question}
      value={typed[question.id] || ''} autoComplete="off"
      data-testid={(question.choices.length ? 'agent-approval-other-input-' : 'agent-approval-input-') + question.id}
      onChange={(event) => setTyped({...typed, [question.id]: event.target.value})} />
  );
  return (
    <div className="acard__q" data-testid={'agent-question-' + question.id}>
      {question.header ? <b>{question.header}</b> : null}
      <span>{question.question}</span>
      {question.choices.length ? (
        <div className="aopts" role="radiogroup" aria-label={question.question}>
          {question.choices.map((c, i) => (
            <button key={c.label} type="button" role="radio" aria-checked={choice === c.label}
              className={'aopt' + (choice === c.label ? ' on' : '')} data-testid={'agent-option-' + question.id + '-' + i}
              onClick={() => setPicked({...picked, [question.id]: c.label})}>
              <span className="aopt__tx">
                <span className="aopt__l">{c.label}</span>
                {c.description ? <span className="aopt__d">{c.description}</span> : null}
              </span>
              {choice === c.label ? <Icon name="check" size={14} /> : null}
            </button>
          ))}
          {question.other ? (
            <button type="button" role="radio" aria-checked={choice === OTHER}
              className={'aopt' + (choice === OTHER ? ' on' : '')} data-testid={'agent-option-' + question.id + '-other'}
              onClick={() => setPicked({...picked, [question.id]: OTHER})}>
              <span className="aopt__tx"><span className="aopt__l">{t.agent.questionOther}</span></span>
              {choice === OTHER ? <Icon name="check" size={14} /> : null}
            </button>
          ) : null}
          {choice === OTHER ? field : null}
        </div>
      ) : field}
    </div>
  );
}

// Options stacked as full-width rows with their descriptions, "Other…" when
// codex allows it, a password field for a secret answer, and several
// questions one at a time, then a review (Collie's wizard, kept on the card:
// here the card is the answer channel).
// AGENT-QUEUE-1 (Leo: "askuserquestion 的卡片 会循环"): a question card's
// progress, kept outside the card by request id and the questions themselves.
// codex re-sends a request it is still waiting on when the thread is resumed;
// if this page's answer never reached it, the same card came back at question
// 1 with nothing chosen. Now it comes back where the person was - after an
// answer, at "Check your answers" (a secret answer is never kept).
const questionDrafts = new Map();
export function questionsFingerprint(approval) {
  return approval.questions.map((q) => [q.id, q.header || '', q.question].concat(q.choices.map((c) => c.label)).join('\u001f'))
    .join('\u001e');
}
export function savedDraft(approval) {
  const draft = questionDrafts.get(approval.requestID);
  return draft && draft.fp === questionsFingerprint(approval) ? draft : null;
}
export function keepDraft(approval, draft) {
  questionDrafts.delete(approval.requestID);
  questionDrafts.set(approval.requestID, {...draft, fp: questionsFingerprint(approval)});
  while (questionDrafts.size > 16) questionDrafts.delete(questionDrafts.keys().next().value);
}

function Questions({approval, onAnswer, t}) {
  const saved = savedDraft(approval);
  const [picked, setPicked] = useState(() => (saved ? saved.picked : {}));
  const [typed, setTyped] = useState(() => (saved ? saved.typed : {}));
  const [at, setAt] = useState(() => (saved ? Math.min(saved.at, approval.questions.length) : 0));
  useEffect(() => { keepDraft(approval, {picked, typed, at}); }, [picked, typed, at]); // eslint-disable-line react-hooks/exhaustive-deps
  const questions = approval.questions;
  const deciding = approval.state.kind === 'deciding';
  const values = {};
  for (const q of questions) values[q.id] = valueOf(q, picked, typed);
  const input = A.submission(values);
  const many = questions.length > 1;
  const reviewing = many && at >= questions.length;
  const answerButton = (
    <button type="button" className="fbtn fbtn--primary" disabled={deciding || !Object.keys(input).length || undefined}
      data-testid={'agent-approval-answer-' + approval.requestID}
      onClick={() => {
        // Should the same request come back, it comes back at the review.
        const keep = (map) => Object.fromEntries(Object.entries(map).filter(([id]) => !questions.some((q) => q.id === id && q.secret)));
        keepDraft(approval, {picked: keep(picked), typed: keep(typed), at: many ? questions.length : 0});
        onAnswer(approval.requestID, values);
      }}>{t.agent.answer}</button>
  );
  const body = {picked, typed, setPicked, setTyped, t};
  if (!many) {
    return (
      <div className="acard__qs">
        {questions.map((q) => <QuestionBody key={q.id} question={q} {...body} />)}
        <div className="acard__acts">{answerButton}{deciding ? <span className="spin" aria-hidden="true" /> : null}</div>
      </div>
    );
  }
  return (
    <div className="acard__qs">
      <span className="acard__step" data-testid={'agent-question-step-' + approval.requestID}>
        {reviewing ? t.agent.questionReview : fill(t.agent.questionStep, at + 1, questions.length)}
      </span>
      {reviewing ? (
        <div className="areview" data-testid={'agent-question-review-' + approval.requestID}>
          {questions.map((q) => (
            <div key={q.id} className="acard__line">
              <span>{q.header || q.question}</span>
              <code>{!values[q.id].trim() ? t.agent.questionUnanswered : q.secret ? t.agent.questionHidden : values[q.id]}</code>
            </div>
          ))}
        </div>
      ) : <QuestionBody question={questions[at]} {...body} />}
      <div className="acard__acts">
        {at > 0 ? (
          <button type="button" className="fbtn" data-testid={'agent-question-back-' + approval.requestID}
            onClick={() => setAt(at - 1)}>{t.agent.questionBack}</button>
        ) : null}
        {reviewing ? answerButton : (
          <button type="button" className="fbtn fbtn--primary" data-testid={'agent-question-next-' + approval.requestID}
            onClick={() => setAt(at + 1)}>{at === questions.length - 1 ? t.agent.questionReview : t.agent.questionNext}</button>
        )}
        {deciding ? <span className="spin" aria-hidden="true" /> : null}
      </div>
    </div>
  );
}

// AGENT-COLLIE-1: what a waiting file-change prompt allows - its files and
// line counts - and a way to the change cards above.
function ChangeSummary({approval, row, t, onSee}) {
  const changes = row && row.changes ? row.changes : null;
  const files = changes ? changes.map((c) => c.path) : approval.changeFiles;
  if (!files.length) return null;
  const added = changes ? changes.reduce((n, c) => n + c.added, 0) : approval.changeAdded;
  const removed = changes ? changes.reduce((n, c) => n + c.removed, 0) : approval.changeRemoved;
  const count = files.length + ((row && row.changesOmitted) || 0);
  return (
    <div className="achgsum" data-testid={'agent-change-summary-' + approval.requestID}>
      <span>
        {(count === 1 ? t.agent.changeFile : fill(t.agent.changeFiles, count)) + ' · '}
        <span className="achg__n" aria-label={fill(t.agent.changeLinesLabel, added, removed)}>
          <span className="achg__add">{'+' + added}</span> <span className="achg__del">{'−' + removed}</span>
        </span>
      </span>
      <span className="achgsum__f">{files.map(S.fileName).join(', ')}</span>
      {changes && onSee ? (
        <button type="button" className="tlink" data-testid={'agent-change-see-' + approval.requestID}
          onClick={onSee}>{t.agent.changeSee}</button>
      ) : null}
    </div>
  );
}

export function ApprovalCard({approval, onDecide, onAnswer, t, row = null, onSee = null, mark = '', onDeclineWhy = null}) {
  const [expanded, setExpanded] = useState(false);
  // AGENT-QUEUE-1: "No, and here's why" - one optional line under the decisions.
  const [why, setWhy] = useState(null);
  const state = approval.state;
  if (state.kind === 'resolved') {
    const declined = state.decision === 'decline' || state.decision === 'cancel';
    // AGENT-COLLIE-1: an answered question says what this page sent (never a
    // secret answer); otherwise just that it was answered.
    const name = approval.answered && state.source === 'client' ? fill(t.agent.answeredWith, approval.answered.join(', '))
      : state.decision ? decisionLabel(state.decision, t) : t.agent.answered;
    return (
      <div className="aresolved" data-testid={'agent-approval-resolved-' + approval.requestID}>
        <span className={'aresolved__g' + (declined ? '' : ' ok')} aria-hidden="true">{declined ? '–' : '✓'}</span>
        <span className="aresolved__s">{marked(approval.summary || kindTitle(approval.kind, t), mark)}</span>
        <span className="grow" />
        <span className="aresolved__d">{state.source === 'client' ? name : fill(t.agent.answeredOnHost, name)}</span>
      </div>
    );
  }
  const deciding = state.kind === 'deciding';
  return (
    <div className="acard" data-testid={'agent-approval-' + approval.requestID}>
      <span className="acard__t">{kindTitle(approval.kind, t)}</span>
      {/* A file change's summary is core's own "Apply file changes" unless
          codex gave a reason; the title already says that. */}
      {approval.summary && (approval.kind !== 'fileChange' || approval.details.some((line) => line.key === 'reason'))
        ? <pre className="acard__s">{approval.summary}</pre> : null}
      {approval.kind === 'fileChange' ? <ChangeSummary approval={approval} row={row} t={t} onSee={onSee} /> : null}
      {approval.details.length ? (
        <div className="acard__det">
          <button type="button" className="acard__more" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}
            data-testid={'agent-approval-details-' + approval.requestID}>
            <Icon name={expanded ? 'down' : 'right'} size={12} />{t.agent.details}
          </button>
          {expanded ? approval.details.map((line) => (
            <div key={line.key} className="acard__line"><span>{line.label}</span><code>{line.value}</code></div>
          )) : null}
        </div>
      ) : null}
      {approval.decisions.length ? (
        <div className="acard__acts">
          {approval.decisions.map((decision) => (
            <button key={decision} type="button" className="fbtn" disabled={deciding || undefined}
              data-testid={'agent-approval-' + decision + '-' + approval.requestID}
              onClick={() => onDecide(approval.requestID, decision)}>{decisionLabel(decision, t)}</button>
          ))}
          {deciding ? <span className="spin" aria-hidden="true" /> : null}
        </div>
      ) : <Questions approval={approval} onAnswer={onAnswer} t={t} />}
      {onDeclineWhy && approval.decisions.includes('decline') && !deciding ? (why === null ? (
        <button type="button" className="tlink" data-testid={'agent-approval-why-' + approval.requestID}
          onClick={() => setWhy('')}>{t.agent.declineWhy}</button>
      ) : (
        <form className="acard__why" onSubmit={(event) => { event.preventDefault(); onDeclineWhy(approval.requestID, why); }}>
          <input className="acard__in" value={why} maxLength={2000} autoFocus enterKeyHint="send"
            placeholder={t.agent.declinePlaceholder} aria-label={t.agent.declinePlaceholder}
            data-testid={'agent-approval-why-field-' + approval.requestID} onChange={(event) => setWhy(event.target.value)} />
          <div className="acard__acts">
            <button type="submit" className="fbtn" data-testid={'agent-approval-why-send-' + approval.requestID}>{t.agent.decline}</button>
            <button type="button" className="tlink" onClick={() => setWhy(null)}>{t.agent.cancel}</button>
          </div>
        </form>
      )) : null}
    </div>
  );
}

function Handoff({model, store, t}) {
  const handoff = model.handoff;
  if (handoff.state === 'none') return null;
  const plan = handoff.plan;
  return (
    <div className="ahand" data-testid="agent-handoff-card">
      <b>{t.agent.handoffKeep}</b>
      {handoff.state === 'required' ? (
        <>
          <p>{t.agent.handoffDetail}</p>
          <button type="button" className="tlink" onClick={store.prepareHandoff}>{t.agent.handoffReview}</button>
        </>
      ) : null}
      {handoff.state === 'preparing' ? <p className="busy">{t.agent.checkingSession}</p> : null}
      {handoff.state === 'prepared' || handoff.state === 'confirming' ? (
        <>
          <p>{plan.agent_id + ' · ' + plan.provider}</p>
          <p>{plan.impact}</p>
          <details><summary>{t.agent.handoffRollback}</summary><p>{plan.rollback}</p></details>
          {handoff.state === 'confirming' ? <p className="busy">{t.agent.connectingConversation}</p> : (
            <div className="acard__acts">
              <button type="button" className="tlink" data-testid="agent-handoff-confirm"
                onClick={store.confirmHandoff}>{t.agent.handoffConfirm}</button>
              <button type="button" className="tlink" onClick={store.deferHandoff}>{t.agent.handoffLater}</button>
            </div>
          )}
        </>
      ) : null}
      {handoff.state === 'problem' ? (
        <>
          <p className="warnt">{agentErrorText(t, {code: handoff.code})}</p>
          <button type="button" className="tlink" onClick={store.prepareHandoff}>{t.agent.handoffAgain}</button>
        </>
      ) : null}
    </div>
  );
}

// AGENT-NEW-1: "New conversation" - one warm sentence and one button, in the
// stream like the handoff card; while a turn runs or a question waits it says
// why it cannot start, and its button is off.
function NewConversationCard({model, store, t}) {
  const fresh = model.fresh;
  if (fresh.state === 'none') return null;
  const refusal = A.newRefusal(model);
  return (
    <div className="ahand" data-testid="agent-new-card">
      <b>{t.agent.newConversation}</b>
      {fresh.state === 'starting' ? <p className="busy">{t.agent.newStarting}</p> : (
        <>
          <p data-testid="agent-new-body">{refusal ? t.agent[refusal] : t.agent.newBody}</p>
          {fresh.state === 'problem' ? <p className="warnt">{agentErrorText(t, {code: fresh.code})}</p> : null}
          <div className="acard__acts">
            <button type="button" className="fbtn fbtn--primary" data-testid="agent-new-confirm"
              disabled={!!refusal || undefined} onClick={store.confirmNew}>{t.agent.newStart}</button>
            <button type="button" className="tlink" data-testid="agent-new-later" onClick={store.cancelNew}>{t.agent.newLater}</button>
          </div>
        </>
      )}
    </div>
  );
}

function noticeText(t, notice) {
  if (notice.code) return agentReason(t, notice.code);
  return notice.key ? t.agent[notice.key] || t.attach[notice.key] : notice.text;
}

// AGENT-PLAN-1: what `/diff` brought - the files this conversation changed,
// as the change cards codex's own edits make (never a file viewer; a path
// opens on the computer). Closed with its own button.
export function DiffCard({diff, t, onLink = null, onClose}) {
  return (
    <div className="ahand adiff" data-testid="agent-diff" role="group" aria-label={t.agent.diffTitle}>
      <div className="adiff__h">
        <b>{t.agent.diffTitle}</b>
        <span className="grow" />
        <button type="button" className="pushed__x" aria-label={t.agent.diffClose} data-testid="agent-diff-close" onClick={onClose}>
          <Icon name="close" size={12} />
        </button>
      </div>
      {diff.changes.length ? (
        <ChangeCards row={{itemID: 'diff', status: 'succeeded', changes: diff.changes}} t={t} onLink={onLink} waiting={false} mark="" />
      ) : <p data-testid="agent-diff-empty">{t.agent.diffEmpty}</p>}
      {diff.omitted ? <p className="dim">{fill(t.agent.diffOmitted, diff.omitted)}</p> : null}
    </div>
  );
}

function ModelPicker({model, store, t}) {
  const catalog = model.models;
  const offered = catalog && Array.isArray(catalog.models) ? catalog.models.filter((row) => !row.hidden) : [];
  const effectiveModel = A.effectiveModel(model);
  const effectiveEffort = A.effectiveEffort(model);
  return (
    <div className="apick" data-testid="agent-model-picker">
      <h3 className="apick__h">{t.agent.model}</h3>
      {model.modelsLoading ? <p className="busy">{t.agent.loadingModels}</p> : null}
      {model.modelsNotice ? <p className="warnt">{t.agent[model.modelsNotice]}</p> : null}
      {offered.map((row) => (
        <div key={row.id} className="apick__row">
          <div className="apick__t">
            <b>{row.display_name || row.model || row.id}</b>
            {row.is_default ? <span className="apick__d">{t.agent.default}</span> : null}
            <span className="grow" />
            {effectiveModel === row.id ? <Icon name="check" size={14} /> : null}
          </div>
          {row.description ? <p className="apick__x">{row.description}</p> : null}
          <div className="acard__acts">
            {(row.efforts || []).map((effort) => (
              <button key={effort} type="button"
                className={'fbtn' + (effectiveModel === row.id && effectiveEffort === effort ? ' on' : '')}
                data-testid={'agent-effort-' + row.id + '-' + effort}
                onClick={() => store.choose(row.id, effort)}>{effort}</button>
            ))}
          </div>
        </div>
      ))}
      {model.selectedModel || model.selectedEffort ? (
        <button type="button" className="tlink" onClick={() => store.choose(null, null)}>{t.agent.modelThread}</button>
      ) : null}
    </div>
  );
}

// AGENT-COLLIE-1: a run of three or more steps (Collie's ToolGroup). Folded,
// it is one line - what kinds of steps, how many failed, and the newest step.
// Working (or held open because it was working while on screen) it shows its
// newest steps under "N earlier steps"; the person's own tap wins over both.
export function Run({run, mode, t, onChoose, renderEntry}) {
  const failed = S.runFailed(run);
  const summary = S.runSummary(t, S.runCounts(run));
  const badge = failed ? <span className="arun__bad">{fill(t.agent.runFailed, failed)}</span> : null;
  if (mode === 'folded') {
    const tools = S.runTools(run);
    const last = tools[tools.length - 1];
    return (
      <button type="button" className="arun arun--folded" aria-expanded={false} data-testid={'agent-run-' + run.id}
        aria-label={summary + (failed ? ', ' + fill(t.agent.runFailed, failed) : '')} title={t.agent.runExpandHint}
        onClick={() => onChoose(true)}>
        <span className="arun__h"><Icon name="right" size={12} /><span className="arun__s">{summary}</span>{badge}</span>
        {last ? <span className="arun__last">{S.stepText(t, last)}</span> : null}
      </button>
    );
  }
  const start = mode === 'all' ? 0 : S.tailStart(run, run.waiting);
  const earlier = S.stepsBefore(run, start);
  return (
    <div className="arun" data-testid={'agent-run-' + run.id}>
      {mode === 'all' ? (
        <button type="button" className="arun__fold" aria-expanded data-testid={'agent-run-fold-' + run.id}
          onClick={() => onChoose(false)}>
          <Icon name="down" size={12} /><span className="arun__s">{summary}</span>{badge}
          <span className="grow" /><span className="dim">{t.agent.runFold}</span>
        </button>
      ) : earlier > 0 ? (
        <button type="button" className="arun__fold" aria-expanded={false} data-testid={'agent-run-earlier-' + run.id}
          onClick={() => onChoose(true)}>
          <Icon name="right" size={12} />
          <span className="arun__s">{earlier === 1 ? t.agent.runEarlier : fill(t.agent.runEarlierMany, earlier)}</span>
        </button>
      ) : null}
      {run.entries.slice(start).map((entry) => renderEntry(entry, true))}
    </div>
  );
}

// Find in the conversation (Collie's find bar): Enter = next, Shift+Enter =
// previous, Escape closes; next and previous wrap at the ends.
export function FindBar({query, setQuery, count, current, onStep, onClose, t}) {
  const input = useRef(null);
  useEffect(() => { if (input.current) input.current.focus(); }, []);
  const label = query.trim() ? (count ? (current + 1) + '/' + count : '0/0') : '';
  return (
    <div className="afind" data-testid="agent-find-bar">
      <Icon name="search" size={14} />
      <input ref={input} className="afind__in" type="text" inputMode="search" autoComplete="off" spellCheck={false}
        placeholder={t.agent.findPlaceholder} aria-label={t.agent.findPlaceholder} value={query}
        data-testid="agent-find-input"
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') { event.preventDefault(); onStep(!event.shiftKey); }
          else if (event.key === 'Escape') { event.preventDefault(); onClose(); }
        }} />
      <span className="afind__n" data-testid="agent-find-count" role="status"
        aria-label={query.trim() ? (count ? fill(t.agent.findCountLabel, current + 1, count) : t.agent.findNone) : undefined}>
        {label}
      </span>
      <button type="button" className="pushed__x" aria-label={t.agent.findPrevious} disabled={!count || undefined}
        data-testid="agent-find-previous" onClick={() => onStep(false)}><Icon name="up" size={14} /></button>
      <button type="button" className="pushed__x" aria-label={t.agent.findNext} disabled={!count || undefined}
        data-testid="agent-find-next" onClick={() => onStep(true)}><Icon name="down" size={14} /></button>
      <button type="button" className="pushed__x" aria-label={t.agent.findClose} data-testid="agent-find-close"
        onClick={onClose}><Icon name="close" size={12} /></button>
    </div>
  );
}

// `focus` is what a push named (`ref`: approval, input or done).
// AGENT-QUEUE-1: codex's checklist (its `update_plan`), pinned over the
// conversation while a step is open or its turn runs: "Steps · 2 of 3 ·
// Change the port", opened to the whole list with a tap. codex 0.160 sends it
// only when its config turns `update_plan` on; otherwise there is none and
// nothing is drawn.
export function Checklist({list, t}) {
  const [open, setOpen] = useState(false);
  const line = A.checklistLine(list);
  const word = {completed: t.agent.stepDone, inProgress: t.agent.stepNow, pending: t.agent.stepTodo};
  return (
    <div className="asteps" data-testid="agent-steps">
      <button type="button" className="asteps__h" aria-expanded={open} data-testid="agent-steps-toggle"
        aria-label={(open ? t.agent.stepsHide : t.agent.stepsShow) + ' · ' + fill(t.agent.stepsProgress, line.done, line.total)}
        onClick={() => setOpen(!open)}>
        <Icon name="plan" size={14} />
        <b>{t.agent.stepsTitle}</b>
        <span className="asteps__n" data-testid="agent-steps-progress">{fill(t.agent.stepsProgress, line.done, line.total)}</span>
        <span className="asteps__c">{line.current}</span>
        <Icon name={open ? 'down' : 'right'} size={12} />
      </button>
      {open ? (
        <ol className="asteps__l">
          {list.explanation ? <li className="asteps__x">{list.explanation}</li> : null}
          {list.steps.map((step, index) => (
            <li key={index} className={'asteps__s asteps__s--' + step.status} aria-label={step.step + ' · ' + word[step.status]}>
              <span aria-hidden="true">{step.status === 'completed' ? '✓' : step.status === 'inProgress' ? '›' : '○'}</span>
              <span>{step.step}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

// AGENT-QUEUE-1: what waits in codex's queue, above the composer: "2 waiting"
// and each message with Send now, Edit and Delete. Edit changes it in place.
export function QueueRows({queue, running, t, onItem}) {
  const [editing, setEditing] = useState(null);
  const [words, setWords] = useState('');
  if (!queue || !queue.length) return null;
  return (
    <div className="aqueue" data-testid="agent-queue" role="group" aria-label={fill(t.agent.queueCount, queue.length)}>
      <p className="aqueue__h">
        <b data-testid="agent-queue-count">{fill(t.agent.queueCount, queue.length)}</b>
        <span className="dim">{running ? t.agent.queueWhenEnds : t.agent.queueWaiting}</span>
      </p>
      {queue.map((item) => (editing === item.id ? (
        <form key={item.id} className="aqueue__i aqueue__i--edit" data-testid={'agent-queue-edit-' + item.id}
          onSubmit={async (event) => {
            event.preventDefault();
            if (!words.trim()) return;
            if (await onItem(item.id, 'edit', words)) setEditing(null);
            else setEditing(null);
          }}>
          <textarea className="composer__in" rows={2} value={words} autoFocus aria-label={t.agent.queueLabel}
            data-testid={'agent-queue-field-' + item.id} onChange={(event) => setWords(event.target.value)} />
          <div className="acard__acts">
            <button type="submit" className="fbtn" disabled={!words.trim() || undefined}
              data-testid={'agent-queue-save-' + item.id}>{t.agent.queueSave}</button>
            <button type="button" className="tlink" onClick={() => setEditing(null)}>{t.agent.cancel}</button>
          </div>
        </form>
      ) : (
        <div key={item.id} className="aqueue__i" data-testid={'agent-queue-item-' + item.id} aria-label={t.agent.queueLabel}>
          <p className="aqueue__tx">{item.text}</p>
          <div className="aqueue__a">
            <button type="button" className="fbtn" data-testid={'agent-queue-send-' + item.id}
              onClick={() => onItem(item.id, 'send')}>{t.agent.queueSendNow}</button>
            {item.clipped ? null : (
              <button type="button" className="fbtn" data-testid={'agent-queue-edit-button-' + item.id}
                onClick={() => { setWords(item.text); setEditing(item.id); }}>{t.agent.queueEdit}</button>
            )}
            <button type="button" className="fbtn" data-testid={'agent-queue-delete-' + item.id} aria-label={t.agent.queueDelete}
              onClick={() => onItem(item.id, 'delete')}>{t.agent.queueDelete}</button>
          </div>
        </div>
      )))}
    </div>
  );
}

export default function AgentPanel({client, base, hostId, focus, face = null, WebSocketImpl = globalThis.WebSocket}) {
  const {t} = useCopy();
  const store = useAgent({client, base, WebSocketImpl});
  const model = store.model;
  // LINKS-1: web addresses open here, files on the computer.
  const links = useHostOpen({client});
  const composerRef = useRef(null);
  // AGENT-UI-2: one line at the box's text size - the whole sentence, or the short one.
  const placeholder = useFittingPlaceholder(composerRef, [t.agent.composer, t.agent.composerShort]);
  const target = useRef(() => ({kind: 'agent'})).current;
  const att = useAttachments({client, target, getDraft: store.getDraft, setDraft: store.setDraft, inputRef: composerRef});
  const drop = useDropTarget(att, enterSendsDefault());
  const [picker, setPicker] = useState(false);
  const [pushed, setPushed] = useState(() => A.pushRef(focus));
  const bottom = useRef(null);
  const list = useRef(null);
  // AGENT-NEW-1 (Leo): the panel opens at the newest message and stays there
  // while output streams - unless the person scrolled up to read.
  const pinned = useRef(true);
  const enterSends = useRef(wantsEnterToSend());
  // AGENT-COLLIE-1: find your place. `away` - the reader scrolled up from the
  // end (the jump button shows); `unseen` - output arrived meanwhile (its dot).
  const [away, setAway] = useState(false);
  const [unseen, setUnseen] = useState(false);
  const [findOpen, setFindOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [current, setCurrent] = useState(null);
  const [focusId, setFocusId] = useState(null);
  const [scrollTo, setScrollTo] = useState(null);
  // AGENT-COLLIE-1: the person's own open/fold per run, and the runs that were
  // working while on screen (they stay open when they finish).
  const [choices, setChoices] = useState({});
  const held = useRef(new Set());
  useEffect(() => setPushed(A.pushRef(focus)), [focus]);
  useEffect(() => {
    const onKey = (event) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && (event.key === 'f' || event.key === 'F')) {
        event.preventDefault();
        setFindOpen(true);
      }
    };
    if (typeof document === 'undefined') return undefined;
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
  // A conversation that is (re)loaded or replaced is read from its end.
  const sessionKey = model.identity ? model.identity.providerSessionID : '';
  useEffect(() => { pinned.current = true; }, [sessionKey, picker]);
  const last = model.rows.length ? model.rows[model.rows.length - 1] : null;
  const tail = last ? (last.kind === 'message' ? last.text.length : (last.detail || '').length + ':' + last.status) : 0;
  // AGENT-QUEUE-1: earlier messages arrive above what is being read; the
  // reader stays on the same line, and nothing new is "unseen".
  const anchor = useRef(null);
  const seenTail = useRef('');
  const firstId = model.rows.length ? model.rows[0].id : '';
  useLayoutEffect(() => {
    const el = list.current;
    const kept = anchor.current;
    if (!el || !kept || el.scrollHeight === kept.height) return;
    el.scrollTop = kept.top + (el.scrollHeight - kept.height);
    anchor.current = null;
  }, [firstId]);
  const loadEarlier = () => {
    const el = list.current;
    if (el) anchor.current = {height: el.scrollHeight, top: el.scrollTop};
    store.loadEarlier();
  };
  useEffect(() => {
    const tailKey = (last ? last.id : '') + ':' + tail + ':' + model.approvals.length;
    const grew = tailKey !== seenTail.current;
    seenTail.current = tailKey;
    if (!pinned.current) {
      if (grew) setUnseen(true);
      return;
    }
    const el = list.current;
    if (el) el.scrollTop = el.scrollHeight;
    else if (bottom.current && bottom.current.scrollIntoView) bottom.current.scrollIntoView({block: 'end'});
  }, [model.rows.length, tail, model.approvals.length, model.localEcho, model.phase, model.fresh.state, model.diff, picker]);
  // AGENT-COLLIE-1: find, "See the change" and your messages bring their
  // entry into view once it is drawn (its run open).
  useEffect(() => {
    if (!scrollTo || !list.current) return;
    const el = list.current.querySelector('[data-entry="' + (globalThis.CSS && CSS.escape ? CSS.escape(scrollTo.id) : scrollTo.id) + '"]');
    if (el && el.scrollIntoView) el.scrollIntoView({block: 'center'});
  }, [scrollTo]);

  const effectiveModel = A.effectiveModel(model);
  const effectiveEffort = A.effectiveEffort(model);
  const identity = model.identity;
  const pushTitle = pushed ? {approval: t.next.waitingApproval, input: t.next.waitingInput, done: t.next.waitingDone}[pushed] : null;
  const notice = model.notice;

  const running = model.phase === 'running' || model.phase === 'cancelling';
  const chipsOut = att.chips.length > 0;
  // AGENT-PLAN-1: Plan mode is the thread's (codex says it); the chip shows it.
  const planOn = A.planOn(model);
  const actionable = A.actionablePlan(model);

  // AGENT-COLLIE-1: the drawn conversation, runs of steps folded.
  const entries = A.timeline(model);
  const order = entries.map((entry) => entry.id);
  const waitingApprovals = A.waitingApprovals(model);
  const waitingItems = new Set(waitingApprovals.map((a) => a.itemID).filter(Boolean));
  const items = S.groupRuns(entries);
  const matches = findOpen ? S.findMatches(t, entries, query) : [];
  const matchSet = new Set(matches);
  const currentId = findOpen && current !== null && matches.includes(current) ? current : null;
  const mark = findOpen ? query.trim() : '';
  const rowById = new Map(model.rows.map((row) => [row.id, row]));
  const jumpTo = (id) => {
    if (!id) return;
    pinned.current = false;
    setAway(true);
    setFocusId(id);
    setScrollTo({id, n: Date.now()});
  };
  const stepFind = (forward) => {
    const next = S.step(matches, order, currentId, forward);
    if (next) { setCurrent(next); jumpTo(next); }
  };
  const closeFind = () => { setFindOpen(false); setQuery(''); setCurrent(null); setFocusId(null); };
  // New words: the first match, from the top, is the one on screen (as the App).
  useEffect(() => {
    if (!findOpen || !query.trim()) return;
    const first = S.step(matches, order, null, true);
    if (first) { setCurrent(first); jumpTo(first); }
  }, [findOpen, query]); // eslint-disable-line react-hooks/exhaustive-deps
  const toNewest = () => {
    pinned.current = true;
    setAway(false);
    setUnseen(false);
    const el = list.current;
    if (el) el.scrollTop = el.scrollHeight;
  };
  const renderEntry = (entry, compact = false) => {
    const cls = 'aentry' + (matchSet.has(entry.id) ? ' afind-hit' : '') + (entry.id === currentId ? ' afind-cur' : '');
    const entryMark = matchSet.has(entry.id) ? mark : '';
    let body;
    if (entry.kind === 'answered') {
      body = <ApprovalCard approval={entry.approval} onDecide={store.decide} onAnswer={store.answer} t={t} mark={entryMark} />;
    } else if (entry.row.kind === 'message') {
      const plan = entry.row.plan && entry.row.id === actionable ? {
        refusal: A.modeRefusal(model) ? t.agent[A.modeRefusal(model)] : null,
        onImplement: () => { pinned.current = true; store.implementPlan(t.agent.planImplementMessage); },
        onKeep: () => { if (composerRef.current) composerRef.current.focus(); }} : null;
      body = (
        <Message row={entry.row} streaming={A.streaming(model, entry.row)} t={t} onLink={links.openLink} mark={entryMark}
          plan={plan} />
      );
    } else {
      body = (
        <ToolRow row={entry.row} t={t} onLink={links.openLink} compact={compact} mark={entryMark}
          waiting={waitingItems.has(entry.row.itemID)} />
      );
    }
    return <div key={entry.id} className={cls} data-entry={entry.id}>{body}</div>;
  };
  // A run holding what find, "See the change" or your messages points at
  // opens for it.
  const runMode = (run) => {
    const live = S.runLive(run, waitingItems);
    if (live) held.current.add(run.id);
    if (focusId && run.entries.some((entry) => entry.id === focusId)) return 'all';
    return S.runMode(choices[run.id], live, held.current.has(run.id));
  };
  // ATTACH-1: what rides with Send - the uploaded chips' ids and markers.
  const sendNow = () => {
    if (att.uploading) return att.say('wait');
    pinned.current = true;  // the person wants to see the answer
    const images = att.chips.filter((chip) => chip.status === 'ready' && chip.kind === 'image').length;
    store.send(chipsOut ? {refs: att.refs(), images, onSent: att.clear} : null);
    return undefined;
  };
  // AGENT-QUEUE-1: while a turn runs, Send waits in codex's queue; a codex
  // without one is steered into, as before.
  const queueing = running && model.queue !== null;
  const submit = () => {
    if (running) {
      if (chipsOut) return;
      if (queueing) store.queue(); else store.steer();
    } else sendNow();
  };

  return (
    <section className="apanel" data-testid="panel-agent" aria-label={t.panels.agent} {...drop.handlers}>
      <DropVeil over={drop.over} t={t} />
      <header className="apanel__head">
        {/* MOTION-1: the agent's own place - its face, with the shade. */}
        <AgentFace surface="header" state={face ? face.state : 'idle'} tool={!!(face && face.tool)} size={32}
          label={fill(t.face.label, faceWord(t, face))} testid="agent-face-header" />
        <span className="apanel__id" data-testid="agent-identity">
          <b>{identity ? identity.agentID + ' · ' + identity.provider : t.agent.default}</b>
          <span>{statusLine(model, t)}</span>
        </span>
        <span className="grow" />
        {/* AGENT-PLAN-1: Plan mode on - a small chip; a tap goes back to the
            default mode. While a turn runs it says why it cannot, rather than
            being a dimmed control that looks broken. */}
        {planOn ? (
          <PlanChip refusal={A.modeRefusal(model) ? t.agent[A.modeRefusal(model)] : null} t={t}
            onLeave={() => store.setMode('default')} />
        ) : null}
        <Usage usage={model.usage} t={t} />
        {/* AGENT-COLLIE-1: find in the conversation (also Ctrl/Cmd+F). */}
        <button type="button" className={'fbtn apanel__new' + (findOpen ? ' on' : '')} aria-pressed={findOpen}
          aria-label={t.agent.find} title={t.agent.find} data-testid="agent-find" disabled={!identity || undefined}
          onClick={() => (findOpen ? closeFind() : setFindOpen(true))}>
          <Icon name="search" size={14} />
        </button>
        {/* AGENT-NEW-1: a quiet header control, not a big button. While a turn
            runs or a question waits, the card it opens says why and keeps its
            own button off (the App's A-65: never a dimmed control that works). */}
        <button type="button" className={'fbtn apanel__new' + (model.fresh.state !== 'none' ? ' on' : '')}
          disabled={!identity || undefined} aria-pressed={model.fresh.state !== 'none'}
          aria-label={t.agent.newConversation} title={A.newRefusal(model) ? t.agent[A.newRefusal(model)] : t.agent.newConversation}
          data-testid="agent-new" onClick={() => {
            setPicker(false);
            if (model.fresh.state === 'none') store.askNew(); else store.cancelNew();
          }}>
          <Icon name="newChat" size={14} />
        </button>
        <button type="button" className={'fbtn apanel__model' + (picker ? ' on' : '')} disabled={!identity || undefined}
          aria-label={fill(t.agent.modelValue, effectiveModel || '—', effectiveEffort || '—')} aria-pressed={picker}
          data-testid="agent-model-control" onClick={() => {
            setPicker(!picker);
            if (!picker) store.loadModels();
          }}>
          {/* AGENT-UI-2 (Leo: "模型和effort 一行全是折叠"): the model on one
              line and the effort under it, so a phone reads both whole. */}
          <span className="apanel__mvs">
            <span className="apanel__mv">
              {effectiveModel || t.agent.model}
              {model.selectedModel || model.selectedEffort ? <span aria-hidden="true"> ↑</span> : null}
            </span>
            {effectiveEffort ? <span className="apanel__me dim" data-testid="agent-model-effort">{effectiveEffort}</span> : null}
          </span>
        </button>
      </header>
      {pushTitle ? (
        <div className="pushed" data-testid="agent-from-push">
          <Icon name="bell" size={14} />
          <span className="pushed__t">{pushTitle}</span>
          <span className="grow" />
          <a className="pushed__app" href={appLinks('agent', hostId).universal} target="_blank" rel="noopener noreferrer"
            data-testid="agent-open-app">{t.next.openApp}</a>
          <button type="button" className="pushed__x" aria-label={t.agent.dismissResult} onClick={() => setPushed(null)}>
            <Icon name="close" size={12} />
          </button>
        </div>
      ) : null}
      {picker ? <ModelPicker model={model} store={store} t={t} /> : (
        <>
          {findOpen ? (
            <FindBar query={query} setQuery={(value) => { setQuery(value); setCurrent(null); }} count={matches.length}
              current={currentId ? matches.indexOf(currentId) : -1} onStep={stepFind} onClose={closeFind} t={t} />
          ) : null}
          <div className="apanel__wrap">
          {A.checklistShown(model) ? <Checklist list={A.checklistShown(model)} t={t} /> : null}
          <div className="apanel__list" data-testid="agent-transcript" ref={list}
            onScroll={(event) => {
              const el = event.currentTarget;
              // AGENT-QUEUE-1: near the top with more before - the next page.
              if (el.scrollTop < 80 && model.earlier && !model.loadingEarlier) loadEarlier();
              pinned.current = A.atEnd(el.scrollTop, el.clientHeight, el.scrollHeight);
              if (pinned.current) {
                if (away) setAway(false);
                if (unseen) setUnseen(false);
              } else if (!away) setAway(true);
            }}>
            <div className="apanel__col">
              {model.earlier ? (
                <button type="button" className="tlink aearlier" data-testid="agent-earlier" disabled={model.loadingEarlier || undefined}
                  onClick={loadEarlier}>{model.loadingEarlier ? t.agent.earlierLoading : t.agent.earlier}</button>
              ) : null}
              {/* AGENT-FIX-2: an answered prompt is one line under the row it
                  was about; only a prompt still waiting is at the end.
                  AGENT-COLLIE-1: three or more steps in a row are one run. */}
              {items.map((item) => (item.kind === 'entry' ? renderEntry(item.entry) : (
                <Run key={item.id} run={{...item, waiting: waitingItems}} mode={runMode(item)} t={t}
                  renderEntry={renderEntry}
                  onChoose={(open) => { setChoices({...choices, [item.id]: open}); if (!open && focusId) setFocusId(null); }} />
              )))}
              {waitingApprovals.map((approval) => {
                const row = approval.itemID ? rowById.get('tool:' + approval.itemID) || null : null;
                return (
                  <ApprovalCard key={approval.requestID} approval={approval} onDecide={store.decide} onAnswer={store.answer} t={t}
                    row={row} onSee={row ? () => jumpTo(row.id) : null} onDeclineWhy={store.declineWithReason} />
                );
              })}
              {!model.rows.length && !model.approvals.length ? (
                <>
                  <div className="apanel__art">
                    <AgentFace surface="large" state={face ? face.state : 'idle'} tool={!!(face && face.tool)} size={96}
                      testid="agent-face-large" />
                  </div>
                  <p className="apanel__empty" data-testid="agent-empty">{model.phase === 'starting' ? t.agent.starting : t.agent.continue}</p>
                </>
              ) : null}
              {model.phase === 'reconnecting' ? (
                <button type="button" className="tlink" data-testid="agent-reconnect" onClick={store.retry}>{t.agent.reconnect}</button>
              ) : null}
              {model.localEcho ? <Message row={model.localEcho} streaming={false} t={t} /> : null}
              {A.awaitingFirstToken(model) ? <p className="busy" data-testid="agent-thinking">{t.agent.thinking}</p> : null}
              <Handoff model={model} store={store} t={t} />
              <NewConversationCard model={model} store={store} t={t} />
              {model.diff ? <DiffCard diff={model.diff} t={t} onLink={links.openLink} onClose={store.closeDiff} /> : null}
              {model.error && model.handoff.state === 'none' ? (
                <div className="aerr" data-testid="agent-error">
                  <p className="warnt">{agentErrorText(t, model.error)}</p>
                  <button type="button" className="tlink" onClick={store.retry}>{t.agent.retry}</button>
                </div>
              ) : null}
              <a className="apanel__app" href={appLinks('agent', hostId).universal} target="_blank"
                rel="noopener noreferrer" data-testid="agent-open-app-quiet">{t.next.openApp}</a>
              <i ref={bottom} />
            </div>
          </div>
          {away ? (
            <button type="button" className="ajump" aria-label={t.agent.jumpNewest} data-testid="agent-jump-newest"
              title={unseen ? t.agent.jumpUnseen : t.agent.jumpNewest} onClick={toNewest}>
              <Icon name="down" size={16} />
              {unseen ? <i className="ajump__dot" role="img" aria-label={t.agent.jumpUnseen} data-testid="agent-jump-unseen" /> : null}
            </button>
          ) : null}
          </div>
          <div className="acomp">
            <LinkNotice notice={links.notice} t={t} hostId={hostId} onDismiss={links.dismiss}
              onComputer={links.openURLOnComputer} testid="agent-link-notice" />
            {notice ? (
              <div className="anotice" data-testid="agent-command-result">
                <p>{noticeText(t, notice)}</p>
                <button type="button" className="pushed__x" aria-label={t.agent.dismissResult} onClick={store.dismissNotice}>
                  <Icon name="close" size={12} />
                </button>
              </div>
            ) : null}
            <QueueRows queue={model.queue} running={running} t={t} onItem={store.queueItem} />
            <AttachNotice att={att} t={t} testid="agent-attach-notice" />
            <AttachmentChips att={att} draft={model.draft} t={t} testid="agent-chips" />
            <div className="composer">
              <div className="composer__box">
              <AttachButton att={att} t={t} testid="agent-attach" disabled={!identity || running} />
              <textarea ref={composerRef} className="composer__in" rows={1} placeholder={placeholder} aria-label={t.agent.composer}
                value={model.draft} data-testid="agent-chat-composer" onPaste={att.onPaste}
                onChange={(event) => store.setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && enterSends.current) {
                    event.preventDefault();
                    submit();
                  }
                }} />
              </div>
              {model.phase === 'running' || model.phase === 'cancelling' ? (
                <>
                  {queueing ? (
                    <button type="button" className="fbtn fbtn--primary composer__b" data-testid="agent-queue-send"
                      disabled={!A.canQueue(model) || chipsOut || undefined} onClick={store.queue}>{t.agent.send}</button>
                  ) : (
                    <button type="button" className="fbtn composer__b" data-testid="agent-steer"
                      disabled={!A.canSteer(model) || chipsOut || undefined} onClick={store.steer}>{t.agent.steer}</button>
                  )}
                  <button type="button" className="fbtn composer__b" data-testid="agent-interrupt"
                    disabled={!A.canCancel(model) || undefined} onClick={store.cancel}>{t.agent.interrupt}</button>
                </>
              ) : (
                <button type="button" className="fbtn fbtn--primary composer__b" data-testid="agent-send"
                  disabled={!A.canSend(model, att.ready) || att.uploading || undefined}
                  title={att.uploading ? t.attach.wait : undefined} onClick={sendNow}>{t.agent.send}</button>
              )}
            </div>
          </div>
        </>
      )}
    </section>
  );
}
