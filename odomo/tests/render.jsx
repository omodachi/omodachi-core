// TAIL-3b: draws panels to static markup in Node, for applink.test.mjs.
// Loaded through Vite's module runner (the same JSX transform as the build),
// so the test needs no browser and no second toolchain.
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {Copy} from '../src/copy.js';
import {LANGUAGES} from '../src/lib/i18n.js';
import NextPanel from '../src/panels/NextPanel.jsx';
import MenuPanel from '../src/panels/MenuPanel.jsx';
import AgentPanel from '../src/panels/AgentPanel.jsx';
import HerdrPanel, {ChoicesStrip, NewWorkspaceCard} from '../src/panels/HerdrPanel.jsx';

// ODOMO-2: the two panels drawn once, with a client that never answers (the
// effects that would call it do not run in a static render).
const idle = new Proxy({}, {get: () => () => new Promise(() => {})});
export function renderAgent(props, lang = 'en') {
  return renderToStaticMarkup(
    <Copy.Provider value={{t: LANGUAGES[lang], lang}}>
      <AgentPanel client={idle} base="https://h.example/" {...props} />
    </Copy.Provider>,
  );
}
export function renderHerdr(props, lang = 'en') {
  return renderToStaticMarkup(
    <Copy.Provider value={{t: LANGUAGES[lang], lang}}>
      <HerdrPanel client={idle} base="https://h.example/" theme={null} revision={0} {...props} />
    </Copy.Provider>,
  );
}

export function renderNext(props, lang = 'en') {
  return renderToStaticMarkup(
    <Copy.Provider value={{t: LANGUAGES[lang], lang}}>
      <NextPanel {...props} />
    </Copy.Provider>,
  );
}

export function renderMenu(props, lang = 'en') {
  return renderToStaticMarkup(
    <Copy.Provider value={{t: LANGUAGES[lang], lang}}>
      <MenuPanel layout={{twoColumn: false}} column="menu" onColumn={() => {}} onKeybindings={() => {}} {...props} />
    </Copy.Provider>,
  );
}

// LITE-1c: the bar and Settings.
import Sidebar from '../src/Sidebar.jsx';
import SettingsPanel from '../src/panels/SettingsPanel.jsx';

export function renderSidebar(props, lang = 'en') {
  return renderToStaticMarkup(
    <Sidebar t={LANGUAGES[lang]} panel="menu" panels={['remote', 'agent', 'herdr', 'settings', 'notifications']}
      status="connected" state={null} notifications={[]} dnd={false} onLogo={() => {}} onPanel={() => {}}
      onWorkspace={() => {}} {...props} />,
  );
}

export function renderSettings(props, lang = 'en') {
  return renderToStaticMarkup(
    <Copy.Provider value={{t: LANGUAGES[lang], lang}}>
      <SettingsPanel client={{prefs: () => new Promise(() => {})}} state={null} status="connected"
        credential={{device_id: 'odomo-1', host_name: 'omarchy'}} build="b" language={null} onLanguage={() => {}}
        onForget={() => {}} {...props} />
    </Copy.Provider>,
  );
}

// LITE-1e: the install screen, its steps and the offer.
import InstallScreen, {InstallOffer, InstallSteps} from '../src/Install.jsx';
import PushSettings from '../src/panels/PushSettings.jsx';

function wrap(node, lang) {
  return renderToStaticMarkup(<Copy.Provider value={{t: LANGUAGES[lang], lang}}>{node}</Copy.Provider>);
}
export const renderInstall = (props, lang = 'en') => wrap(<InstallScreen device="iPhone" {...props} />, lang);
export const renderSteps = (lang = 'en') => wrap(<InstallSteps />, lang);
export const renderOffer = (props, lang = 'en') => wrap(<InstallOffer {...props} />, lang);
export const renderPush = (lang = 'en') => wrap(<PushSettings client={{prefs: () => new Promise(() => {})}} />, lang);

// HERDR-6: the new-workspace card, drawn from a store in a given state.
export function renderNewWorkspace(store, lang = 'en') {
  return renderToStaticMarkup(
    <Copy.Provider value={{t: LANGUAGES[lang], lang}}>
      <NewWorkspaceCard store={store} t={LANGUAGES[lang]} />
    </Copy.Provider>,
  );
}

// LINKS-1: an agent reply and the line a tapped link leaves.
import Markdown from '../src/Markdown.jsx';
import LinkNotice from '../src/LinkNotice.jsx';
export function renderMarkdown(text) {
  return renderToStaticMarkup(<Markdown text={text} onLink={() => false} />);
}
export function renderLinkNotice(notice, lang = 'en', hostId = null) {
  return renderToStaticMarkup(
    <LinkNotice notice={notice} t={LANGUAGES[lang]} hostId={hostId} onDismiss={() => {}} onComputer={() => {}}
      testid="agent-link-notice" />,
  );
}

// NOTIFY-3: the Notifications panel, and its event reducer.
import NotificationsPanel, {notificationEvent, requestNote} from '../src/panels/NotificationsPanel.jsx';
export {notificationEvent, requestNote};
export function renderNotifications(props, lang = 'en') {
  return renderToStaticMarkup(
    <Copy.Provider value={{t: LANGUAGES[lang], lang}}>
      <NotificationsPanel dnd={false} onDnd={() => {}} {...props} />
    </Copy.Provider>,
  );
}

// TUTORIAL-1: one stop of the tour, drawn once (no window: the callout is
// centred, its effects do not run).
import Tour from '../src/Tour.jsx';
export function renderTour(state, lang = 'en') {
  return renderToStaticMarkup(
    <Copy.Provider value={{t: LANGUAGES[lang], lang}}>
      <Tour state={state} onNext={() => {}} onSkip={() => {}} />
    </Copy.Provider>,
  );
}

// HERDR-7: the pane column and the chip strip, drawn from a layout.
import {GridColumn, PaneStrip} from '../src/panels/HerdrPanel.jsx';
export function renderHerdrPanes(layout, wide = true, lang = 'en', overview = undefined) {
  const store = {layout, selected: null, stopped: null, loadingLayout: false, selectWorkspace: () => {},
    select: () => {}, openNewWorkspace: () => {}, selectedSession: layout && layout.session};
  const Panes = wide ? GridColumn : PaneStrip;
  return wrap(<Panes store={store} t={LANGUAGES[lang]} numbered={false} overview={overview} />, lang);
}

// OVERVIEW-1: the "What wants you" card on its own.
import {OverviewCard} from '../src/panels/HerdrPanel.jsx';
export function renderOverview(overview, lang = 'en') {
  return wrap(<OverviewCard overview={overview} t={LANGUAGES[lang]} onClose={() => {}} onRow={() => {}} />, lang);
}

// HERDR-8: the wrapped mirror, drawn from a layout.
import {WrapView} from '../src/panels/HerdrPanel.jsx';
export function renderWrap(result, palette) {
  return renderToStaticMarkup(<WrapView result={result} palette={palette} onLink={() => {}} onDoubleTap={() => {}}
    onSingleTap={() => {}} />);
}

// HERDR-9: the choices strip on its own, for a store's choices.
export function renderChoices(store, lang = 'en') {
  return renderToStaticMarkup(<ChoicesStrip store={store} t={LANGUAGES[lang]} />);
}

// AGENT-COLLIE-1: the Agent panel's pieces, drawn with rows a static render of
// the whole panel cannot have.
import {ToolRow, ApprovalCard, Run, FindBar} from '../src/panels/AgentPanel.jsx';
export const renderToolRow = (props, lang = 'en') => wrap(<ToolRow t={LANGUAGES[lang]} onLink={() => {}} {...props} />, lang);
export const renderApproval = (props, lang = 'en') =>
  wrap(<ApprovalCard t={LANGUAGES[lang]} onDecide={() => {}} onAnswer={() => {}} {...props} />, lang);
export const renderRun = (props, lang = 'en') =>
  wrap(<Run t={LANGUAGES[lang]} onChoose={() => {}} renderEntry={(entry) => <i key={entry.id} data-entry={entry.id} />} {...props} />, lang);
export const renderFindBar = (props, lang = 'en') =>
  wrap(<FindBar t={LANGUAGES[lang]} setQuery={() => {}} onStep={() => {}} onClose={() => {}} {...props} />, lang);

// AGENT-PLAN-1: the Plan chip and the plan card.
import {PlanCard, PlanChip, DiffCard} from '../src/panels/AgentPanel.jsx';
export const renderPlanCard = (props, lang = 'en') => wrap(<PlanCard t={LANGUAGES[lang]} onLink={() => {}} {...props} />, lang);
export const renderPlanChip = (props, lang = 'en') => wrap(<PlanChip t={LANGUAGES[lang]} onLeave={() => {}} {...props} />, lang);
export const renderDiffCard = (props, lang = 'en') => wrap(<DiffCard t={LANGUAGES[lang]} onClose={() => {}} {...props} />, lang);

// AGENT-QUEUE-1: the queue above the composer, the pinned checklist.
import {QueueRows, Checklist} from '../src/panels/AgentPanel.jsx';
export const renderQueue = (props, lang = 'en') => wrap(<QueueRows t={LANGUAGES[lang]} onItem={() => {}} {...props} />, lang);
export const renderChecklist = (props, lang = 'en') => wrap(<Checklist t={LANGUAGES[lang]} {...props} />, lang);
// AGENT-QUEUE-1: the question card's kept progress.
import {keepDraft, savedDraft} from '../src/panels/AgentPanel.jsx';
export const keepQuestionDraft = (approval, draft) => keepDraft(approval, draft);
export const savedQuestionDraft = (approval) => savedDraft(approval);
