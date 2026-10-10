// Every string Odomo shows, in English and Simplified Chinese.
//
// Provenance: the English half began as omodachi-web `src/app/copy.js`
// (main 0da28c9, same author), which carries the `en` values of
// omodachi-ios `Omodachi/Resources/Localizable.xcstrings`. The Chinese half
// is that catalog's own `zh-Hans` (its source language) wherever the App has
// the string; the key is named beside each line. The rest - pairing, the
// connection, the placeholders - is Odomo's own and has no App key.
//
// Both tables must have exactly the same keys (tests/i18n.test.mjs).
//
// The web app's own name is APP_NAME (lib/name.js), filled into every
// sentence that says it, so renaming it is one line.

import {APP_NAME} from './name.js';


export const en = {
  panels: {
    menu: 'Omarchy menu', //            panel.menu
    agent: 'Agent', //                  panel.agent
    herdr: 'Herdr', //                  panel.herdr
    remote: 'Remote', //                panel.remote
    settings: 'Settings', //            panel.settings
    notifications: 'Notifications', //  panel.notifications
    keybindings: 'Keybindings', //      panel.keybindings
    panelTab: 'Panel', //               panel.panelTab
  },
  bar: {
    label: 'Omodachi bar',
    logo: 'Omarchy menu and Keybindings', // bar.logo.label
    connection: 'Host connection', //         bar.connection
    connected: 'Connected', //                bar.connection.connected
    connecting: 'Connecting',
    offline: 'Offline',
    // HERDR-11: the one connection clock (App connection.*).
    reconnecting: 'Reconnecting…', //          connection.reconnecting
    connectingTo: 'Connecting to %s…', //      connection.connecting
    deviceOffline: 'This device is offline', // connection.deviceOffline
    unreachable: 'Can’t reach %s', //          connection.hostUnreachable
    herdrDown: 'Herdr isn’t running on %s', // connection.herdrDown
    tryAgain: 'Try again', //                  connection.tryAgain
    workspaces: 'Workspaces', //              bar.workspaces
    workspace: 'Workspace %s', //             bar.workspace.name
    current: 'Current', //                    bar.workspace.current
    occupied: 'Has windows', //               bar.workspace.occupied
    empty: 'Empty', //                        bar.workspace.empty
    unread: '%s unread', //                   badge.unread
    panels: 'Panels',
    // LITE-1c: the word under each icon where the bar has room for one, and
    // what Remote in the bar says on iPhone and iPad, where it is the link
    // into the app.
    names: {menu: 'Menu', herdr: 'Herdr', agent: 'Agent', remote: 'Remote', notifications: 'Notifications', settings: 'Settings'},
    opensApp: '%s · opens the app',
  },
  menu: {
    search: 'Search Omarchy…', //                     menu.search
    clear: 'Clear search', //                         menu.search.clear
    group: 'OMARCHY MENU', //                         menu.group
    results: 'Results', //                            menu.results
    rootPath: 'Omarchy menu', //                      menu.rootPath
    noMatch: 'No menu item matches', //               menu.empty.noMatch
    noMatchHint: 'Search covers the merged menu.', // menu.empty.searchHint
    tapAgain: 'Tap again to run', //                  menu.tapAgain
    unavailable: 'Unavailable', //                    menu.unavailable
    sent: 'Sent. Waiting for the host.', //           keybindings.sent
    ran: 'Done on the host',
    failed: 'The host did not run it',
    inApp: 'In the app',
    loading: 'Reading the menu from the host…',
    pinned: 'PINNED', //                              pinned.title
    edit: 'Edit', //                                  pinned.edit
    done: 'Done', //                                  pinned.editDone
    pinnedEmpty: 'Nothing pinned yet.', //            pinned.empty
    pinnedMenuHint: 'Press and hold a row, or use Edit to show the pins.', // pinned.empty.menuHint
    pinnedKeyHint: 'Press and hold a binding, or use Edit to show the pins.', // pinned.empty.keyHint
    pin: 'Pin %s', //                                 pinned.pin
    unpin: 'Unpin %s', //                             pinned.unpin
    pinsLocal: 'Pins live in this browser only.',
    gone: 'Gone from the host', //                    menu.gone
  },
  keys: {
    search: 'Search actions or shortcuts…', //        keybindings.search
    noMatch: 'No matching actions.', //               keybindings.noMatch
    runHint: 'Run this action on the host', //        keybindings.runHint
    unavailable: 'The host cannot replay this binding.',
  },
  notifications: {
    dnd: 'Do Not Disturb', //                         notifications.dnd
    dndOn: 'Do Not Disturb is on: no banners, and notifications still arrive here.', // notifications.dnd.on
    all: 'All', //                                    notifications.all
    unread: '%s unread', //                           notifications.unreadCount
    history: 'History', //                            notifications.history
    hide: 'Hide this notification here',
    clear: 'Clear all', //                           notifications.clear
    decideOnComputer: 'This is decided on the computer: click the notification there, or open Omodachi › Devices. It goes from here once it is decided.', // notifications.request.onComputer
    empty: 'No notifications on the computer yet.', // notifications.empty
    emptyDetail: 'Everything the host raises lands here.', // notifications.emptyDetail
    live: 'Live: new ones appear while this page is open.',
  },
  next: {
    remoteTitle: 'Remote is in the app',
    remoteBody: 'The extra screen and taking over the desktop are in the Omodachi app for iPhone and iPad.',
    openRemote: 'Open Remote in the app',
    haveApp: 'Already have the app? Open it',
    // NOTIFY-2 / ODOMO-2: what a push was about, on top of the panel it
    // opened (the push titles' own words), and the quiet way into the app.
    openApp: 'Open in Omodachi',
    waitingApproval: 'The agent needs your approval',
    waitingInput: 'The agent is waiting for you',
    waitingDone: 'The agent is done',
    waitingHerdr: 'A pane in Herdr is waiting for you',
    // HERDR-4: the agent's name and the session, never a raw pane id.
    waitingWhereAgent: 'It is %s, in the %s session.',
    waitingWhereSession: 'It is in the %s session.',
  },
  // ODOMO-2: Odomo's Herdr panel. The App's own words (omodachi-ios
  // Localizable.xcstrings, herdr.*), key by key, so both clients say the
  // same thing; %s are filled in order (session shape: %1 %2 %3).
  herdr: {
    session: 'Herdr session', //                      herdr.session
    sessionStart: 'Start %s', //                      herdr.session.start
    sessionStartDetail: 'Stopped on the computer. Starts with no window, as you left it.', // herdr.session.start.detail
    sessionStarting: 'Starting %s on the computer…', // herdr.session.starting
    sessionStopped: 'Stopped', //                     herdr.session.stopped
    sessionSwitching: 'Switching session…', //        herdr.session.switching
    sessionUnreadable: 'Not answering', //            herdr.session.unreadable
    sessionEmpty: 'No pane yet', //                   herdr.session.empty
    sessionOwned: 'Made by Omodachi', //              herdr.session.owned
    sessionShape: '%1 workspaces · %2 panes · %3 agents', // herdr.session.shape
    sessionNew: 'New session…', //                    herdr.session.new
    sessionNewDetail: 'A new Herdr session on the computer', // herdr.session.new.detail
    newTitle: 'New Herdr session', //                 herdr.newSession.title
    newDetail: 'It starts in your home folder on the computer, and keeps running after you leave.', // herdr.newSession.detail
    newPlaceholder: 'A name, like work', //           herdr.newSession.placeholder
    newCreate: 'Create', //                           herdr.newSession.create
    newCreating: 'Creating…', //                      herdr.newSession.creating
    // HERDR-6: a new workspace, in a folder the person picks.
    wsTitle: 'New workspace', //                      herdr.newWorkspace.title
    wsDetail: 'A new workspace in %s, starting in the folder you pick. The computer stays on what it is showing.', // herdr.newWorkspace.detail
    wsPath: 'Folder', //                              herdr.newWorkspace.path
    wsPathPlaceholder: 'Folder (empty: your home folder)', // herdr.newWorkspace.path.placeholder
    wsBrowse: 'Browse', //                            herdr.newWorkspace.browse
    wsUp: 'Up', //                                    herdr.newWorkspace.up
    wsHidden: 'Show hidden', //                       herdr.newWorkspace.hidden
    wsRecent: 'Recent', //                            herdr.newWorkspace.recent
    wsCommon: 'Common', //                            herdr.newWorkspace.common
    wsLoading: 'Looking for your folders…', //        herdr.newWorkspace.loading
    wsEmpty: 'No folders in here.', //                herdr.newWorkspace.empty
    wsTruncated: 'Showing the first 100. Type the rest of the path to go further.', // herdr.newWorkspace.truncated
    wsName: 'Name', //                                herdr.newWorkspace.name
    wsNamePlaceholder: 'Name (optional, the folder\'s by default)', // herdr.newWorkspace.name.placeholder
    // HERDR-11: what the new workspace starts with, and starred folders.
    wsStart: 'Start in it', //                        herdr.newWorkspace.start
    wsStartShell: 'Just a shell', //                  herdr.newWorkspace.start.shell
    wsStartDetail: 'Only this one command is typed, into the new workspace’s first pane. Nothing else is touched.', // herdr.newWorkspace.start.detail
    wsFavourites: 'Favourites', //                    herdr.newWorkspace.favourites
    wsFavourite: 'Add to favourites', //              herdr.newWorkspace.favourite
    wsUnfavourite: 'Remove from favourites', //       herdr.newWorkspace.unfavourite
    wsCreateAndStart: 'Create and start %s', //       herdr.newWorkspace.createAndStart
    launchNotSent: 'The workspace is open, but %s didn’t start. You can start it in the pane yourself.', // herdr.launch.notSent
    lastUpdate: 'Last update %s', //                  herdr.mirror.lastUpdate
    noPanes: 'No pane to observe', //                 herdr.noPanes
    noPanesDetail: 'Open a pane on the host and it appears here.', // herdr.noPanesDetail
    loading: 'Reading…', //                           herdr.loading
    loadingLayout: 'Reading the layout…', //          herdr.loadingLayout
    noWorkspace: 'This session has no workspace.', // herdr.noWorkspace
    noSelectedPane: 'No pane is selected.', //        herdr.noSelectedPane
    onlyOnePane: 'This tab has one pane.', //         herdr.onlyOnePane
    onlyOneTab: 'This workspace has only this tab.', // herdr.onlyOneTab
    focused: 'Focused', //                            herdr.focused
    tab: 'tab %s', //                                 herdr.tab
    zoomed: 'Zoomed', //                              herdr.zoomed
    waiting: 'Waiting for you', //                    herdr.waiting
    paneNumbered: 'Pane %s', //                       herdr.pane.numbered
    type: 'Type', //                                  herdr.type
    typeHint: 'While it is on, every key you press goes straight into this pane.', // herdr.type.hint
    typeStrip: 'Typing straight into %s. Tap the screen to stop.', // herdr.type.strip
    typingIdleOff: 'No keys for a while, so typing is off for now.', // herdr.type.idleOff
    stop: 'Stop', //                                  herdr.stop
    quick: 'Quick', //                                herdr.quick
    quickHint: 'One tap sends a common reply or command into this pane.', // herdr.quick.hint
    quickConfirm: 'Confirm', //                       herdr.quick.confirm
    quickCommon: 'Common', //                         herdr.quick.common
    quickCommands: 'Your commands', //                herdr.quick.commands
    quickHarness: 'This agent\'s commands', //       herdr.quick.harness
    harnessModel: 'Model', //                         herdr.harness.model
    harnessEffort: 'Effort', //                       herdr.harness.effort
    harnessCompact: 'Compact', //                     herdr.harness.compact
    harnessResume: 'Resume', //                       herdr.harness.resume
    harnessHint: 'Sends %s, and the agent\'s own picker opens in the pane.', // herdr.harness.hint
    operatorUnparsed: '%s on the computer could not be read, so the last version that worked is still in use.', // herdr.operator.unparsed
    operatorDropped: 'A few rows in %s could not be used, so they were left out.', // herdr.operator.dropped
    keys: 'Keys', //                                  herdr.keys
    presets: 'Key presets', //                        herdr.presets
    keySend: 'Send %s', //                            herdr.key.send
    tapAgain: 'Tap again', //                         herdr.tapAgain
    tapAgainToSend: 'Tap %s again to send it.', //    herdr.tapAgainToSend
    more: 'More', //                                  herdr.more
    newWorkspace: 'New workspace…', //                herdr.action.newWorkspace
    newTab: 'New tab', //                             herdr.action.newTab
    closeTab: 'Close tab', //                         herdr.action.closeTab
    close: 'Close pane', //                           herdr.action.closePane
    closePaneTitle: 'Close this pane?', //            herdr.closePaneTitle
    closePaneDetail: 'The process in this pane ends on the host. Other panes are untouched.', // herdr.closePane.detail
    closeTabTitle: 'Close this tab?', //              herdr.closeTabTitle
    closeTabDetail: 'Every pane in the tab ends, along with its process. Other tabs are not affected.', // herdr.closeTab.detail
    closePaneConfirm: 'Close pane', //                herdr.closePane
    composer: 'Write to this pane', //                herdr.composer.placeholder
    send: 'Send', //                                  herdr.send
    sending: 'Sending…', //                           herdr.sending
    sent: 'Sent ✓', //                                herdr.sent
    sentLight: 'Sent. It is short, so it is hard to be sure it landed; have a look at the pane.', // herdr.sent.light
    notSubmitted: 'Not submitted — %s', //           herdr.notSubmitted
    readOnly: 'To type here, turn on Type, or write in the box below.', // herdr.readOnly
    streamClosed: 'Herdr closed this pane\'s stream (%s).', // herdr.streamClosed
    zoomFit: 'Fit to width', //                       herdr.zoom.fit
    zoomActual: 'Actual size', //                     herdr.zoom.actual
    zoomWrap: 'Wrap', //                              herdr.zoom.wrap
    mirrorHint: 'Switches between wrapped text, the screen fitted to the width, and its actual size.', // herdr.mirror.hint
    mirrorHintPhone: 'Switches between wrapped text and the screen fitted to the width.', // herdr.mirror.hint.phone
    fullScreenNote: 'A full-screen program, shown as its screen', // herdr.fullScreen.note
    keysHint: 'Shows or hides Esc, Tab, the arrows, ^C and the digits.', // herdr.keys.hint
    hintPhone: 'Double-tap the screen to switch between Wrap and Fit to width. Keys holds Esc, the arrows and ^C.', // hint.herdr.phone
    modeObserve: 'watching', //                       herdr.mode.observe
    modeTyping: 'typing', //                          herdr.mode.typing
    modeOffline: 'offline', //                        herdr.mode.offline
    modeRetry: 'retry %s', //                         herdr.mode.retry
    hint: 'Type here, or use the keys below. Type sends every key you press straight in, full-screen apps like vim included. Tap the screen to put the keyboard away; double-tap it to switch between fit to width and actual size.', // hint.herdr
    dismiss: 'Dismiss',
    keyNames: {escape: 'Escape', tab: 'Tab', shiftTab: 'Shift-Tab', enter: 'Return', space: 'Space', up: 'Up arrow',
      down: 'Down arrow', left: 'Left arrow', right: 'Right arrow', backspace: 'Delete', ctrlC: 'Control-C',
      ctrlD: 'Control-D'}, // herdr.key.*
    // HERDR-9: the choices strip.
    choicesTitle: 'The pane is waiting for your answer.', // herdr.choices.title
    choicesHint: 'Sends this answer to the pane, just as pressing it at the computer would.', // herdr.choices.hint
    choicesSelected: 'This is the one selected on the pane right now.', // herdr.choices.selected
    choicesLasting: 'This one keeps applying after this time, not just now.', // herdr.choices.lasting
    choicesEscapeHint: 'Presses Esc to cancel or leave this question.', // herdr.choices.escapeHint
    choicesYes: 'Yes', //                             herdr.choices.yes
    choicesNo: 'No', //                               herdr.choices.no
    choicesAnswered: 'Sent to the pane. Waiting for it to move on.', // herdr.choices.answered
    choicesChanged: 'The question on the pane just changed, so nothing was sent. Take another look, then tap again.', // herdr.choices.changed
    composerHint: 'While the box is empty, Delete, Return and the arrows go straight to the pane.', // herdr.composer.fieldHint (its second half)
  },
  // ODOMO-2: Odomo's Agent panel, the App's words (agent.*, approval.*, tool.*).
  agent: {
    default: 'default', //                            agent.default
    composer: 'Message your agent…', //               agent.composer
    composerShort: 'Message…', //                     (AGENT-UI-2: when the whole line does not fit)
    send: 'Send', //                                  agent.send
    steer: 'Steer', //                                agent.steer
    interrupt: 'Interrupt', //                        agent.interrupt
    dismissResult: 'Dismiss the result', //           agent.dismissResult
    roleYou: 'You', //                                agent.role.you
    roleAgent: 'Agent', //                            agent.role.agent
    starting: 'Starting your default agent…', //      agent.starting
    continue: 'Continue with your default agent.', // agent.continue
    thinking: 'Thinking…', //                         agent.thinking
    reconnect: 'Reconnect', //                        agent.reconnect
    retry: 'Try again', //                            action.retry
    details: 'Details', //                            agent.details
    answer: 'Answer', //                              agent.answer
    yourAnswer: 'Your answer', //                     approval.yourAnswer
    accept: 'Accept', //                              approval.accept
    acceptSession: 'Accept for session', //           approval.acceptSession
    decline: 'Decline', //                            approval.decline
    cancel: 'Cancel', //                              action.cancel
    answered: 'answered', //                          approval.answered
    answeredOnHost: '%s on the host', //              approval.answeredOnHost
    kindCommand: 'Run a command', //                  approval.kind.command
    kindFileChange: 'Apply file changes', //          approval.kind.fileChange
    kindPermissions: 'Grant more permissions', //     approval.kind.permissions
    kindUserInput: 'The agent needs your input', //   approval.kind.userInput
    phaseStarting: 'Starting…', //                    agent.phase.starting
    phaseReady: 'Ready', //                           agent.phase.ready
    phaseSending: 'Sending…', //                      agent.phase.sending
    phaseWorking: 'Working…', //                      agent.phase.working
    phaseStopping: 'Stopping…', //                    agent.phase.stopping
    phaseReconnecting: 'Reconnecting…', //            agent.phase.reconnecting
    phaseFailed: 'Needs attention', //                agent.phase.failed
    waitingOnYou: 'Waiting for you', //               agent.waitingOnYou
    needsInput: 'Needs your input', //                agent.needsInput
    waitingCount: '%s waiting', //                    agent.waitingCount
    context: 'Context', //                            agent.context
    model: 'model', //                                agent.model
    modelValue: 'Model %s, effort %s', //             agent.modelValue
    modelThread: 'Follow the thread', //              agent.model.thread
    loadingModels: 'Reading this agent\'s models…', // agent.loadingModels
    modelsUnreadable: 'This agent\'s models could not be read. The next message keeps the current setting.', // agent.modelsUnreadable
    turnFailed: 'That turn failed.', //               agent.turnFailed
    sendUnconfirmed: 'This message is unconfirmed. Reconnect and check the conversation before sending again.', // agent.sendUnconfirmed
    previousSendUnconfirmed: 'The previous message is unconfirmed. Settle it before sending a different one.', // agent.previousSendUnconfirmed
    attachmentGone: 'An attachment is no longer on the computer. Remove it and attach it again.', // attach.error.gone
    pathOnly: 'The agent couldn’t take the picture itself, so it got the file’s path.', // attach.pathOnly
    answerUnconfirmed: 'The host did not confirm that answer. The agent is still waiting.', // agent.answerUnconfirmed
    steerTooLate: 'That turn finished before the interjection arrived. Send it as a message instead.', // agent.steerTooLate
    steerUnconfirmed: 'The interjection is unconfirmed. Your words are unchanged; check the turn before sending again.', // agent.steerUnconfirmed
    stopUnconfirmed: 'Stop is unconfirmed. Reconnect and check the running turn.', // agent.stopUnconfirmed
    handoffKeep: 'Keep the current agent', //         agent.handoff.keep
    handoffDetail: 'The terminal session needs one handoff before native chat. Nothing moves until you confirm.', // agent.handoff.detail
    handoffReview: 'Review the change', //            agent.handoff.review
    handoffConfirm: 'Confirm the handoff', //         agent.handoff.confirm
    handoffLater: 'Not now', //                       agent.handoff.later
    handoffAgain: 'Review again', //                  agent.handoff.again
    handoffRollback: 'If the connection fails', //    agent.handoffRollback
    checkingSession: 'Checking the current session…', // agent.checkingSession
    connectingConversation: 'Connecting the same conversation…', // agent.connectingConversation
    toolStatus: {running: 'running', succeeded: 'succeeded', failed: 'failed', cancelled: 'cancelled',
      interrupted: 'stopped', unknown: 'unknown'}, // tool.status.*
    toolTruncated: '%s of earlier output not shown', // agent.tool.truncated (AGENT-FIX-2)
    // AGENT-COLLIE-1: steps, runs, change cards, questions, find (agent.step.* / run.* / change.* / question.* / find.*)
    stepRead: 'Read', // agent.step.read
    stepListed: 'Listed', // agent.step.listed
    stepSearched: 'Searched', // agent.step.searched
    stepEdited: 'Edited', // agent.step.edited
    stepCreated: 'Created', // agent.step.created
    stepDeleted: 'Deleted', // agent.step.deleted
    stepMoved: 'Moved', // agent.step.moved
    stepAndMore: '%s and %s more', // agent.step.andMore
    toolExit: 'exit %s', // agent.tool.exit
    runCommand: '1 command', // agent.run.command
    runCommands: '%s commands', // agent.run.commands
    runRead: '1 file read', // agent.run.read
    runReads: '%s files read', // agent.run.reads
    runSearch: '1 search', // agent.run.search
    runSearches: '%s searches', // agent.run.searches
    runEdit: '1 edit', // agent.run.edit
    runEdits: '%s edits', // agent.run.edits
    runOther: '1 other step', // agent.run.other
    runOthers: '%s other steps', // agent.run.others
    runFailed: '%s failed', // agent.run.failed
    runEarlier: '1 earlier step', // agent.run.earlier
    runEarlierMany: '%s earlier steps', // agent.run.earlierMany
    runFold: 'Fold these steps', // agent.run.fold
    runExpandHint: 'Shows each step.', // agent.run.expandHint
    changeMore: 'Show %s more lines', // agent.change.more
    changeLess: 'Show fewer lines', // agent.change.less
    changeOmitted: '%s more lines stay on the computer.', // agent.change.omitted
    changeFilesOmitted: '%s more files changed; they are on the computer.', // agent.change.filesOmitted
    changeWaiting: 'Waiting for you to allow this change', // agent.change.waiting
    changeFile: '1 file', // agent.change.file
    changeFiles: '%s files', // agent.change.files
    changeSee: 'See the change', // agent.change.see
    changeLinesLabel: '%s added, %s removed', // agent.change.linesLabel
    questionOther: 'Other…', // agent.question.other
    questionOtherPlaceholder: 'Answer in your own words', // agent.question.otherPlaceholder
    questionStep: 'Question %s of %s', // agent.question.step
    questionBack: 'Back', // agent.question.back
    questionNext: 'Next', // agent.question.next
    questionReview: 'Check your answers', // agent.question.review
    questionHidden: 'Hidden', // agent.question.hidden
    questionUnanswered: 'Not answered yet', // agent.question.unanswered
    questionSelected: 'Selected', // agent.question.selected
    answeredWith: 'Answered: %s', // approval.answeredWith
    find: 'Find', // agent.find
    findPlaceholder: 'Find in the conversation', // agent.find.placeholder
    findCountLabel: 'Match %s of %s', // agent.find.countLabel
    findNone: 'No matches', // agent.find.none
    findPrevious: 'Previous match', // agent.find.previous
    findNext: 'Next match', // agent.find.next
    findClose: 'Close find', // agent.find.close
    jumpNewest: 'Jump to the newest', // agent.jump.newest
    jumpUnseen: 'New output below', // agent.jump.unseen
    runSep: ', ', //                                 pair (the App joins with Strings.pair)
    // AGENT-NEW-1: a new conversation (agent.new.*)
    newConversation: 'New conversation', //            agent.new.action
    newBody: 'This starts a fresh conversation with the agent. The old one stays on the computer.', // agent.new.body
    newStart: 'Start a new conversation', //           agent.new.start
    newLater: 'Not now', //                           agent.new.later
    newStarting: 'Starting a new conversation…', //   agent.new.starting
    newWhileWorking: 'The agent is in the middle of a turn. Let it finish, or stop it, then start a new conversation.', // agent.new.whileWorking
    newWhileWaiting: 'The agent is waiting for your answer. Answer it first, then start a new conversation.', // agent.new.whileWaiting
    newUnavailable: 'The conversation is not connected yet.', // agent.new.unavailable
    // AGENT-PLAN-1: codex's Plan mode (agent.plan.*)
    planChip: 'Plan', //                               agent.plan.chip
    planChipLabel: 'Plan mode is on', //              agent.plan.chipLabel
    planChipHint: 'Tap to go back to the default mode.', // agent.plan.chipHint
    planWhileWorking: 'Plan mode can change once this turn has finished.', // agent.plan.whileWorking
    planSwitching: 'Switching the mode…', //          agent.plan.switching
    planFailed: 'The mode did not change. Try again.', // agent.plan.failed
    planTitle: 'Proposed plan', //                    agent.plan.title
    planImplement: 'Implement this plan', //          agent.plan.implement
    planImplementHint: 'This leaves Plan mode and asks the agent to start.', // agent.plan.implementHint
    // AGENT-QUEUE-1: after a plan, stay and keep shaping it
    planKeep: 'Keep planning', //                      agent.plan.keep
    planKeepHint: 'Stays in Plan mode. Write what to change.', // agent.plan.keepHint
    planImplementMessage: 'Implement the plan.', //   agent.plan.implementMessage
    // AGENT-PLAN-1: what /diff brings (agent.diff.*)
    diffTitle: 'Changes in this conversation', //     agent.diff.title
    diffEmpty: 'This conversation has not changed any files yet.', // agent.diff.empty
    diffOmitted: '%s earlier changes are not shown.', // agent.diff.omitted
    diffClose: 'Close the changes', //                agent.diff.close
    // AGENT-QUEUE-1: messages waiting for the turn to end (agent.queue.*)
    queueCount: '%s waiting', //                      agent.queue.count
    queueWhenEnds: 'They go out when this turn ends.', // agent.queue.whenEnds
    queueWaiting: 'Stopped. Send them when you’re ready.', // agent.queue.waiting
    queueSendNow: 'Send now', //                       agent.queue.sendNow
    queueEdit: 'Edit', //                              agent.queue.edit
    queueDelete: 'Delete', //                          agent.queue.delete
    queueSave: 'Save', //                              agent.queue.save
    queueLabel: 'Waiting message', //                  agent.queue.label
    queueEditGone: 'It had already gone out, so your new words are back in the box.', // agent.queue.editGone
    queueUnconfirmed: 'That didn’t reach the computer. Try again.', // agent.queue.unconfirmed
    // AGENT-QUEUE-1: codex's checklist (agent.steps.*)
    stepsTitle: 'Steps', //                            agent.steps.title
    stepsProgress: '%s of %s', //                      agent.steps.progress
    stepsShow: 'Show the steps', //                    agent.steps.show
    stepsHide: 'Hide the steps', //                    agent.steps.hide
    stepDone: 'done', //                               agent.steps.done
    stepNow: 'in progress', //                         agent.steps.now
    stepTodo: 'to do', //                              agent.steps.todo
    // AGENT-QUEUE-1: no, and here's why (agent.decline.*)
    declineWhy: 'Decline with a reason…', //           agent.decline.why
    declinePlaceholder: 'Why not? One line, optional', // agent.decline.placeholder
    // AGENT-QUEUE-1: huge threads (agent.earlier*, agent.trimmed)
    earlier: 'Earlier messages', //                    agent.earlier
    earlierLoading: 'Loading earlier messages…', //    agent.earlier.loading
    trimmed: '%s more characters are not shown here.', // agent.trimmed
  },
  // ODOMO-2: core's codes, in words (the App's ReasonText, herdr and agent
  // domains). An unmapped code is shown beside a sentence, never alone.
  attach: {
    // ATTACH-1: the App's `attach.*` keys, word for word.
    button: 'Attach', //                                  attach.button
    photos: 'Photos', //                                  attach.photos
    files: 'Files', //                                    attach.files
    imageN: 'Image #%s', //                               attach.chip.image
    fileN: 'File #%s', //                                 attach.chip.file
    uploading: 'Uploading %s', //                         attach.uploading
    remove: 'Remove %s', //                               attach.remove
    dismiss: 'Dismiss', //                                action.dismiss
    markerGone: 'Its marker is gone from your words, so Send puts it first.', // attach.markerGone
    tooLarge: '%s is over 25 MB, too big to send.', //    attach.error.tooLarge
    badType: '%s can’t be sent. Pictures, PDFs and text or code files can.', // attach.error.badType
    svg: 'SVG can’t be sent, because it can carry scripts. A PNG or JPEG works.', // attach.error.svg
    notText: '%s isn’t plain text, so it can’t go as a text file.', // attach.error.notText
    heic: 'The computer couldn’t turn this photo into a JPEG.', // attach.error.heic
    empty: '%s is empty.', //                             attach.error.empty
    gone: 'An attachment is no longer on the computer. Remove it and attach it again.', // attach.error.gone
    failed: '%s didn’t upload. Try again.', //            attach.error.failed
    tooMany: 'One message carries ten attachments at most.', // attach.error.tooMany
    wait: 'Wait for the upload to finish, then send.', // attach.wait
    pathOnly: 'The agent couldn’t take the picture itself, so it got the file’s path.', // attach.pathOnly
    picture: 'With a picture', //                         attach.picture
    pictures: 'With %s pictures', //                      attach.pictures
    drop: 'Drop to attach',
  },
  links: {
    // LINKS-1: links in what agents send.
    opening: 'Opening it on the computer…',
    opened: 'Opened %s on the computer.',
    openedAt: 'Opened %s on the computer, at line %s.',
    watch: 'Watch in Omodachi Remote',
    here: 'Opened %s in a new tab.',
    hereHint: 'If it doesn’t load, the server is probably listening only on the computer itself, so it can’t be reached from here.',
    onComputer: 'Open on the computer',
    dismiss: 'Dismiss',
    image: 'Picture',
    refused: {
      open_not_mentioned: 'That isn’t in what the agent showed any more, so it won’t be opened.',
      open_outside_roots: 'That file is outside your home folder and the agent’s folder, so it stays closed.',
      open_denied: 'That’s a private place (keys, passwords or settings), so it won’t be opened from here.',
      open_executable: 'That’s a program or a script. It won’t be run from here; open it on the computer if you mean to.',
      open_missing: 'That file isn’t on the computer (any more).',
      open_source_gone: 'That message or pane is gone, so there’s nothing to check the link against.',
      open_type_refused: 'That isn’t a file or a folder, so there’s nothing to open.',
      open_url_refused: 'Only web addresses can be opened that way.',
      open_rate_limited: 'That’s a lot of opening in one minute. Try again in a moment.',
      open_unavailable: 'The computer couldn’t open it right now. Is someone signed in at its screen?',
      web_client_forbidden: 'This page can’t open things on the computer.',
      host_unreachable: 'The computer didn’t answer. Try again in a moment.',
    },
    refusedUnknown: 'The computer didn’t open it (%s).',
  },
  reasons: {
    herdrUnknown: 'The host could not finish this Herdr request (%s).', // reason.herdr.unknown
    agentUnknown: 'The host could not finish this agent request (%s).', // reason.agent.unknown
    herdr: {
      herdr_control_in_use: 'Another end is driving this pane. You are watching, read-only.',
      herdr_unavailable: 'The host\'s Herdr session is not answering.',
      herdr_request_failed: 'Herdr refused that action.',
      invalid_pane: 'That pane is no longer in the host\'s layout.',
      invalid_workspace: 'That workspace is no longer in the host\'s layout.',
      invalid_geometry: 'The terminal size is outside what Herdr accepts.',
      herdr_action_unsupported: 'The host\'s Herdr bridge has no route for that action.',
      invalid_key: 'Herdr does not know that key.',
      herdr_key_unsupported: 'Herdr cannot send PageUp, PageDown, Home, End, Insert or Delete.',
      herdr_too_many_keys: 'That is too many keys at once.',
      invalid_text: 'The text has terminal control characters in it, so it was not sent.',
      herdr_text_too_long: 'The text is longer than 16 KB, so it was not sent.',
      herdr_pane_gone: 'That pane is no longer on the computer.',
      not_seen: 'The text never showed up in the pane, so Enter was not pressed. A dialog may have the focus.',
      submit_failed: 'The text got there, but Enter did not go through.',
      not_requested: 'The text got there; Enter was not pressed.',
      herdr_agent_blocked: 'The agent is waiting on a question, so nothing was typed; answer it with the choices above the box, or with the keys (1, ↓ ↩ or esc).',
      invalid_session: 'That session is not on the computer any more.',
      invalid_session_name: 'A name is letters, digits and . _ - only, starting with a letter or a digit, up to 32 characters.',
      herdr_session_exists: 'The computer already has a session by that name. Pick another name, or choose it from the list.',
      herdr_session_start_failed: 'The session did not start on the computer. Try again in a moment.',
      invalid_folder: 'That does not look like a folder. Try a path like ~/Work/project.',
      folder_outside_home: 'Pick a folder inside your home folder on the computer.',
      folder_private: 'That folder holds keys or private settings, so Omodachi will not open a workspace there.',
      folder_not_found: 'There is no folder there on the computer. Check the path, or pick one from the list.',
      folder_unreadable: 'That folder cannot be opened to look inside.',
      invalid_workspace_name: 'A name is one line, up to 64 characters.',
      herdr_launch_not_allowed: 'That isn’t one of this computer’s launchers any more. Close the card and open it again.',
      host_unreachable: 'The host is not answering.',
    },
    agent: {
      existing_thread_handoff_required: 'Your existing agent needs a one-time connection handoff.',
      agent_busy: 'Your agent is working. Its task is still running. Try again when it is idle.',
      agent_pane_busy: 'Your agent is working. Its task is still running. Try again when it is idle.',
      handoff_rolled_back: 'The connection could not be changed. Your terminal and conversation were restored.',
      handoff_plan_expired: 'The session changed or this plan expired. Review a fresh plan before continuing.',
      handoff_target_changed: 'The session changed or this plan expired. Review a fresh plan before continuing.',
      handoff_stop_unconfirmed: 'The handoff result is not confirmed. Check the existing session before trying again.',
      handoff_attach_unconfirmed: 'The handoff result is not confirmed. Check the existing session before trying again.',
      handoff_thread_unconfirmed: 'The handoff result is not confirmed. Check the existing session before trying again.',
      handoff_transport_unavailable: 'The channel this handoff needs is not connected.',
      agent_owner_start_failed: 'The host agent could not start.',
      agent_owner_start_timeout: 'The host agent could not start.',
      agent_owner_socket_unavailable: 'The host agent could not start.',
      thread_creation_unconfirmed: 'The host agent could not start.',
      provider_request_rejected: 'The host agent could not start.',
      structured_agent_kind_unsupported: 'The host agent could not start.',
      agent_state_unknown: 'The host agent could not start.',
      agent_not_working: 'The agent is not running a task.',
      agent_chat_unavailable: 'This host offers no agent conversation.',
      agent_management_unavailable: 'This host offers no agent conversation.',
      agent_approval_unknown: 'The host does not know that approval.',
      agent_approval_decision_invalid: 'The host does not accept that approval choice.',
      default_agent_unset: 'Choose a default agent in the host Omarchy menu first.',
      history_unavailable: 'This conversation\'s history cannot be read.',
      cancellation_pending: 'The host is still stopping this turn. Try again shortly.',
      turn_failed: 'That turn failed.',
      host_unreachable: 'The host is not answering.',
      // AGENT-NEW-1
      agent_turn_active: 'The agent is in the middle of a turn. Let it finish, or stop it, then start a new conversation.',
      agent_approval_pending: 'The agent is waiting for your answer. Answer it first, then start a new conversation.',
      agent_identity_changed: 'This conversation was replaced by a newer one. Showing the new one.',
      agent_conversation_changing: 'A new conversation is starting. Try again in a moment.',
      new_conversation_confirmation_required: 'Confirm before starting a new conversation.',
      agent_thread_creation_unconfirmed: 'The new conversation is not confirmed yet. Try again.',
      // AGENT-PLAN-1
      agent_mode_unavailable: 'Codex on the computer can\'t switch to Plan mode right now.',
      agent_command_while_working: 'That one can run once the agent has finished this turn.',
      invalid_agent_mode: 'That mode does not exist here.',
    },
  },
  pair: {
    title: 'Pair this browser',
    lead: `${APP_NAME} is %s’s own page. It works once the person at that computer approves this browser.`,
    start: 'Ask to pair',
    asking: 'Asking…',
    waitingTitle: 'Waiting for approval',
    waitingBody: 'On the computer, open the Omodachi panel and choose Approve on the card that names this browser.',
    cardSays: 'The card says',
    device: 'Device',
    via: 'Through',
    viaTailscale: 'Tailscale',
    expires: 'This request ends at %s.',
    cancel: 'Cancel', //                               action.cancel
    rejectedTitle: 'Not approved',
    rejectedBody: 'The request was rejected or ran out of time on the computer. You can ask again.',
    revokedTitle: 'This browser is no longer paired',
    revokedBody: 'Its access was removed on the computer, or ran out. Nothing of it is kept here. Pair again to come back.',
    expiredTitle: 'This browser’s pairing ran out',
    again: 'Pair again',
    failed: 'The computer did not answer the request.',
    grants: 'Approving gives this browser the Omarchy menu, keybindings, notifications, Herdr and the agent. Never the screen, never SSH.',
    insecure: 'This page is not on a secure address, so it cannot pair. Open it at the computer’s https://… .ts.net address.',
  },
  settings: {
    host: 'HOST', //                                  gate.sshHostKey.host
    connected: 'Connected', //                        settings.connected
    connecting: 'Connecting',
    offline: 'Offline',
    address: 'Address',
    browser: 'THIS BROWSER',
    label: 'Shown on the computer as',
    deviceId: 'Device',
    expires: 'Credential ends',
    renews: 'It renews itself in its last week while this page is used.',
    forget: 'Forget the pairing here',
    forgetConfirm: 'Forget it',
    forgetNote: 'This only clears this browser. To take its access away, remove it on the computer’s Devices page.',
    language: 'Language', // ODOMO-SETTINGS-1: a row title now, not a group label
    languageAuto: 'Automatic',
    about: 'About', //                                settings.about
    tourGroup: 'Help', //                             settings.tour.group
    tourReplay: 'Show the tour again', //             settings.tour.replay
    tourReplayDetail: 'A short walk along the bar. The tips in Herdr and Agent come back too.', // settings.tour.replayDetail
    build: 'Build', //                                settings.build
    contract: 'Contract', //                          settings.contract
    // LITE-1c: the way to the Omodachi app, and to get it.
    app: 'OMODACHI APP',
    appOpen: 'Open the app',
    appGet: 'Get the app',
    odomo: `${APP_NAME} is served by the computer’s own Omodachi, at this address only. It keeps a credential in this browser and nothing else.`,
    // ODOMO-2: the App's Settings, less Remote's own (mode, backend, placement)
    road: 'How Omodachi reaches it', //                 host.road
    roadTailscale: 'Through Tailscale, as %s. At home and away alike.', // host.road.tailscale
    roadOther: 'At %s.',
    barEntries: 'BAR ENTRIES', //                      settings.barEntries
    barEntriesNote: 'Switching one off removes its slot from this bar.',
    barEntryLabel: '%s\'s bar entry', //              settings.barEntry.label
    barEntryLocked: 'Settings cannot switch itself off', // settings.barEntry.locked
    showUnrunnable: 'Show bindings that cannot run here', // settings.showUnrunnable
    showUnrunnableDetail: 'The host binds these keys to Lua functions with no executable record, so this device can never run them. On, they come back to the list, still greyed.', // settings.showUnrunnable.detail
    licence: `Licence · MIT`,
    licenceDetail: `${APP_NAME} and what it is built from`,
    licenceBody: `${APP_NAME}: MIT, like the rest of omodachi-core. Built from React (MIT), xterm.js’s terminal core @xterm/headless (MIT, © The xterm.js authors) and JetBrains Mono (SIL OFL 1.1). Their licence texts are served beside ${APP_NAME}.`,
    sourceCode: 'Source code', //                      settings.sourceCode
    privacy: 'Privacy', //                             settings.privacy
  },
  push: {
    group: 'NOTIFICATIONS ON THIS DEVICE',
    on: 'Turn on notifications',
    off: 'Turn off notifications',
    turningOn: 'Turning on…',
    stateOn: 'On',
    stateOff: 'Off',
    note: 'The computer sends them itself, encrypted for this browser, through its push service (Apple, Google or Mozilla). They say what needs you, never more.',
    hostOff: 'Turned off on the computer, on its Devices page.',
    addToHome: `On iPhone and iPad, notifications work in the Home Screen app: Share › Add to Home Screen, then open ${APP_NAME} from there.`,
    denied: 'This browser blocks notifications for this page. Allow them in its settings, then come back.',
    unsupported: 'This browser cannot receive push notifications.',
    insecure: 'Notifications need the computer’s https://… .ts.net address.',
    failed: 'Could not turn notifications on (%s).',
    kinds: 'WHAT TO SEND',
    agent_approval: 'The agent needs your approval',
    agent_needs_input: 'The agent is waiting for an answer',
    agent_done: 'The agent finished',
    herdr_blocked: 'An agent in Herdr needs you',
    host_notification: 'Notifications on the computer',
    herdr_done: 'An agent in Herdr finished and you haven’t looked yet',
    snooze: 'Snooze', // ODOMO-SETTINGS-1: a row title now
    snoozeOff: 'Off',
    snooze1: '1 hour',
    snooze8: '8 hours',
    snoozedUntil: 'Nothing is sent until %s.',
    quiet: 'Quiet while the app is in front',
    quietDetail: 'While the Omodachi app is open in front of you and shows the same alert itself, it is not sent here as well. Whatever the app does not alert, like a finished turn, always comes here.',
    desk: 'Alert me even when I’m at the computer',
    deskDetail: 'Off: while you are using the computer, alerts wait. They come here once it has sat idle for two minutes or is locked, and anything you already answered there is left out. Herdr alerts always wait 30 seconds first, and several waiting panes come as one.',
  },
  // LITE-1e: the install screen (iPhone / iPad, before pairing), the offer
  // (Android, desktop) and Settings › Install. %s are filled in order.
  install: {
    title: `Put ${APP_NAME} on your Home Screen`,
    lead: `${APP_NAME} is %s’s own page. On your %s it works as an app on the Home Screen, and that is where you pair it.`,
    whyTitle: 'WHY IT IS WORTH IT',
    whyNotify: 'Notifications reach you even when your %s is locked and the Omodachi app is closed.',
    whyOpen: 'It opens full screen in one tap from your Home Screen, just like an app.',
    whyStays: 'It stays signed in to this computer: you pair it once, and it remembers.',
    whyStore: 'There is nothing to download from a store. It comes from your own computer.',
    howTitle: 'THREE STEPS',
    step1: 'Tap Share',
    step1Detail: 'In Safari it is next to the address bar, either as its own button or inside the menu there. In the Omodachi app’s browser view, it is the Share button in the toolbar.',
    step2: 'Choose Add to Home Screen',
    step2Detail: 'If it is not in the list, tap View More and it will be there.',
    step3: `Open ${APP_NAME} from your Home Screen`,
    step3Detail: 'Pair it there. The person at the computer approves it once, and you are all set.',
    whyFirst: 'Pairing happens only in the Home Screen app. It keeps its own storage on your %s, so a pairing made here in the browser would not come along.',
    oldPairing: 'This browser was paired before. That pairing is not used here any more; you can remove it on the computer’s Devices page.',
    otherApp: 'Opened inside another app, with no Share button in sight? Open this page in Safari first.',
    offerTitle: `${APP_NAME} can live on your Home screen too`,
    offerAndroid: 'It opens full screen in one tap, like an app. Notifications work here in the browser as well, so this one is up to you.',
    offerDesktop: `Notifications already work in this tab. If you like, ${APP_NAME} can also have a window of its own.`,
    androidSteps: `In Chrome, open the ⋮ menu and choose Add to Home screen (or Install app), then open ${APP_NAME} from there. Your pairing comes along.`,
    installButton: `Install ${APP_NAME}`,
    group: 'INSTALL',
    stateInstalled: 'Installed',
    stateBrowser: 'In the browser',
    installedNote: `${APP_NAME} is installed on this device and opens on its own, like an app.`,
    installedNow: `${APP_NAME} is installed. You can open it from your Home screen or your apps.`,
  },
  errors: {
    generic: 'The host answered %s.',
    offline: 'The host is not answering.',
  },
  // TUTORIAL-1: the first-steps tour along the bar. The App's own words
  // (omodachi-ios Localizable.xcstrings, tour.*) where Odomo has the same
  // thing; Remote, Notifications, the bar and Settings say what Odomo has.
  tour: {
    label: 'Tour',
    next: 'Next', //                     tour.next
    skip: 'Skip', //                     tour.skip
    done: 'Done', //                     tour.done
    progress: 'Step %s of %s', //        tour.progress
    steps: {
      bar: {title: 'Your bar', //        tour.bar.title
        body: 'Every panel is one tap away on this bar, and the logo opens your computer\u2019s Omarchy menu. Hold an icon, or rest the pointer on it, to see its name.'},
      remote: {title: 'Remote', //       tour.remote.title
        body: 'Remote opens in the Omodachi app on your iPhone or iPad: your computer\u2019s screen, as one more screen or the whole desktop.'},
      agent: {title: 'Agent', //         tour.agent.title / .body
        body: 'Talk with the agent on your computer, and send it a photo or a file. Before it runs a command or changes a file, it asks you here first.'},
      herdr: {title: 'Herdr', //         tour.herdr.title / .body
        body: 'Watch your agents at work in their panes, and type into any of them. Quick sends the usual replies in one tap.'},
      notifications: {title: 'Notifications', // tour.notifications.title
        body: `Your computer\u2019s notifications, all in one list. Turn on notifications in Settings and ${APP_NAME} tells you when an agent is waiting.`},
      settings: {title: 'Settings', //   tour.settings.title
        body: 'This computer, the bar and the language live here. This tour does too, near the top: Show the tour again.'},
    },
  },
  // OVERVIEW-1: one list of what wants you, across the Agent and every Herdr
  // pane (App overview.*, badge.needsYou, badge.unseen).
  overview: {
    title: 'What wants you', //                       overview.title
    open: 'See what wants you', //                    overview.open
    openHint: 'Lists the agents waiting for you, working and finished, in the Agent and in every session.', // overview.openHint
    empty: 'Nothing needs you right now.', //         overview.empty
    close: 'Collapse', //                             action.collapse
    sections: {needs_you: 'Needs you', working: 'Working', done: 'Done, not seen yet'}, // overview.section.*
    count: {needs_you: 'Needs you %s', working: 'Working %s', done: 'Done %s'}, // overview.count.*
    ask: {
      approve_command: 'Approve a command', //        overview.ask.approveCommand
      approve_files: 'Approve file changes', //       overview.ask.approveFiles
      approve_permissions: 'Approve more access', //  overview.ask.approvePermissions
      approve: 'Approve a request', //                overview.ask.approve
      answer: 'Answer a question', //                 overview.ask.answer
      pick: 'Pick an option', //                      overview.ask.pick
      pickQuestion: 'Pick an option: %s', //          overview.ask.pickQuestion
      yes_no: 'Yes or no', //                         overview.ask.yesNo
      yesNoQuestion: 'Yes or no: %s', //              overview.ask.yesNoQuestion
      working: 'Working on it', //                    overview.ask.working
      yourMove: 'Your move: %s', //                   overview.ask.yourMove
      finished: 'Finished', //                        overview.ask.finished
      failed: 'Stopped on an error', //               overview.ask.failed
    },
    agent: 'Agent', //                                panel.agent
    needsYouBadge: '%s waiting for you', //           badge.needsYou
    chip: {needs_you: 'Something needs you', working: 'Something is working', // overview.chip.*
      done: 'Finished, not looked at yet'}, //        badge.unseen
  },
  // MOTION-1: the animated face says its state in words too.
  face: {
    label: 'Agent: %s',
    states: {idle: 'idle', thinking: 'thinking', working: 'working', tool: 'running a command',
      waiting: 'waiting for you', done: 'done', error: 'stopped with an error', offline: 'offline',
      // INTEGRATE-2: a turn you interrupted; calm, not an error.
      stopped: 'stopped'},
  },
};

export const zh = {
  panels: {
    menu: 'Omarchy 菜单', //            panel.menu
    agent: 'Agent', //                  panel.agent
    herdr: 'Herdr', //                  panel.herdr
    remote: 'Remote', //                panel.remote
    settings: '设置', //                panel.settings
    notifications: '通知', //           panel.notifications
    keybindings: 'Keybindings', //      panel.keybindings
    panelTab: 'Panel', //               panel.panelTab
  },
  bar: {
    label: 'Omodachi 栏',
    logo: 'Omarchy 菜单与 Keybindings', // bar.logo.label
    connection: '与主机的连接', //         bar.connection
    connected: '已连接', //                bar.connection.connected
    connecting: '正在连接',
    offline: '离线',
    reconnecting: '正在重新连接…',
    connectingTo: '正在连接 %s…',
    deviceOffline: '这台设备没联网',
    unreachable: '连不上 %s',
    herdrDown: '%s 上的 Herdr 没在运行',
    tryAgain: '再试一次',
    workspaces: '工作区', //               bar.workspaces
    workspace: '工作区 %s', //             bar.workspace.name
    current: '当前', //                    bar.workspace.current
    occupied: '有窗口', //                 bar.workspace.occupied
    empty: '空', //                        bar.workspace.empty
    unread: '%s 条未读', //                badge.unread
    panels: '面板',
    names: {menu: '菜单', herdr: 'Herdr', agent: 'Agent', remote: 'Remote', notifications: '通知', settings: '设置'},
    opensApp: '%s · 在 App 中打开',
  },
  menu: {
    search: '搜索 Omarchy…', //                     menu.search
    clear: '清除搜索', //                           menu.search.clear
    group: 'OMARCHY 菜单', //                       menu.group
    results: '搜索结果', //                         menu.results
    rootPath: 'Omarchy 菜单', //                    menu.rootPath
    noMatch: '没有匹配的菜单项', //                 menu.empty.noMatch
    noMatchHint: '搜索覆盖合并后的菜单。', //       menu.empty.searchHint
    tapAgain: '再点一次执行', //                    menu.tapAgain
    unavailable: '不可用', //                       menu.unavailable
    sent: '已发出，等主机的结果。', //              keybindings.sent
    ran: '主机已执行',
    failed: '主机没有执行',
    inApp: '在 App 里',
    loading: '正在从主机读取菜单…',
    pinned: 'PINNED', //                            pinned.title
    edit: '编辑', //                                pinned.edit
    done: '完成', //                                pinned.editDone
    pinnedEmpty: '还没有 pin 任何东西。', //        pinned.empty
    pinnedMenuHint: '长按菜单里的一行，或用「编辑」露出图钉。', // pinned.empty.menuHint
    pinnedKeyHint: '长按列表里的一条绑定，或用「编辑」露出图钉。', // pinned.empty.keyHint
    pin: 'pin %s', //                               pinned.pin
    unpin: '取消 pin %s', //                        pinned.unpin
    pinsLocal: 'pin 只存在这个浏览器里。',
    gone: '主机上已不存在',
  },
  keys: {
    search: '搜索动作或快捷键…', //                 keybindings.search
    noMatch: '没有匹配的动作。', //                 keybindings.noMatch
    runHint: '在主机上执行这个动作', //             keybindings.runHint
    unavailable: '主机没法重放这条绑定。',
  },
  notifications: {
    dnd: '勿扰', //                                 notifications.dnd
    dndOn: '勿扰开着：横幅不再弹，通知仍然进这张列表。', // notifications.dnd.on
    all: '全部', //                                 notifications.all
    unread: '未读 %s', //                           notifications.unreadCount
    history: '历史', //                             notifications.history
    hide: '在这里隐藏这条通知',
    clear: '清除全部', //                           notifications.clear
    decideOnComputer: '这要在电脑上决定：点电脑上的这条通知，或打开 Omodachi › 设备。决定之后它会自己从这里消失。', // notifications.request.onComputer
    empty: '电脑上还没有通知。', //                 notifications.empty
    emptyDetail: '主机上弹出的每一条都会到这里。', // notifications.emptyDetail
    live: '实时：页面开着时新通知会直接出现。',
  },
  next: {
    remoteTitle: 'Remote 在 App 里',
    remoteBody: '扩展屏和接管桌面在 iPhone 和 iPad 上的 Omodachi App 里。',
    openRemote: '在 App 里打开 Remote',
    haveApp: '已经装了 App？直接打开',
    openApp: '在 Omodachi 中打开',
    waitingApproval: 'Agent 需要你批准',
    waitingInput: 'Agent 在等你回复',
    waitingDone: 'Agent 完成了',
    waitingHerdr: 'Herdr 里有个 pane 在等你',
    waitingWhereAgent: '是 %s，在 %s 会话里。',
    waitingWhereSession: '在 %s 会话里。',
  },
  herdr: {
    session: 'Herdr 会话',
    sessionStart: '启动 %s',
    sessionStartDetail: '它在电脑上停着；启动时不开窗口，里面照原样回来。',
    sessionStarting: '正在电脑上启动 %s…',
    sessionStopped: '已停止',
    sessionSwitching: '正在切换会话…',
    sessionUnreadable: '没有应答',
    sessionEmpty: '还没有 pane',
    sessionOwned: 'Omodachi 自建',
    sessionShape: '%1 工作区 · %2 pane · %3 agent',
    sessionNew: '新会话…',
    sessionNewDetail: '在电脑上开一个新的 Herdr 会话',
    newTitle: '新的 Herdr 会话',
    newDetail: '它在电脑上你的主目录里开始，你离开之后也会一直开着。',
    newPlaceholder: '名字，比如 work',
    newCreate: '创建',
    newCreating: '正在创建…',
    wsTitle: '新工作区',
    wsDetail: '在 %s 里开一个新工作区，从你选的文件夹开始。电脑屏幕停在原处，不会跳过去。',
    wsPath: '文件夹',
    wsPathPlaceholder: '文件夹（不填就是主目录）',
    wsBrowse: '浏览',
    wsUp: '上一层',
    wsHidden: '显示隐藏的',
    wsRecent: '最近',
    wsCommon: '常用',
    wsLoading: '正在找你的文件夹…',
    wsEmpty: '这里面没有文件夹。',
    wsTruncated: '只列出前 100 个。要往下找，把路径接着打完。',
    wsName: '名字',
    wsNamePlaceholder: '名字（可不填，默认用文件夹名）',
    wsStart: '开好以后启动',
    wsStartShell: '只开 shell',
    wsStartDetail: '只在新工作区的第一个 pane 里输入这一条命令，别处一个字也不碰。',
    wsFavourites: '收藏',
    wsFavourite: '收藏这个文件夹',
    wsUnfavourite: '取消收藏',
    wsCreateAndStart: '创建并启动 %s',
    launchNotSent: '工作区开好了，但 %s 没能启动。在 pane 里自己启动一下就行。',
    lastUpdate: '最后更新 %s',
    noPanes: '没有可观察的 pane',
    noPanesDetail: '在主机上开一个 pane，这里会出现。',
    loading: '正在读取…',
    loadingLayout: '正在读取布局…',
    noWorkspace: '这个会话里没有 workspace。',
    noSelectedPane: '没有选中的 pane。',
    onlyOnePane: '这个 tab 里只有一个 pane。',
    onlyOneTab: '这个 workspace 只有这一个 tab。',
    focused: '聚焦',
    tab: 'tab %s',
    zoomed: '放大',
    waiting: '在等你',
    paneNumbered: '第 %s 个 pane',
    type: '直接输入',
    typeHint: '打开后，你按的每个键都会直接送进这个 pane。',
    typeStrip: '正在直接输入到 %s。点一下画面就停。',
    typingIdleOff: '好一会儿没有按键，直接输入先关掉了。',
    stop: '停止',
    quick: '快捷',
    quickHint: '一点就把常用的回复或命令发进这个 pane。',
    quickConfirm: '确认',
    quickCommon: '常用',
    quickCommands: '你的命令',
    quickHarness: '这个 agent 的命令',
    harnessModel: '模型',
    harnessEffort: '思考力度',
    harnessCompact: '压缩',
    harnessResume: '继续以前的',
    harnessHint: '发送 %s，pane 里会打开 agent 自己的选择器。',
    operatorUnparsed: '电脑上的 %s 读不出来了，所以还在用上一版能用的。',
    operatorDropped: '%s 里有几行用不了，已经跳过了。',
    keys: '按键',
    presets: '预设按键',
    keySend: '发送 %s',
    tapAgain: '再点一次',
    tapAgainToSend: '再点一次 %s 才会发出去。',
    more: '更多',
    newWorkspace: '新工作区…',
    newTab: '新建 tab',
    closeTab: '关掉 tab',
    close: '关掉 pane',
    closePaneTitle: '关闭这个 pane？',
    closePaneDetail: '主机上这个 pane 的进程会结束。其他 pane 不受影响。',
    closeTabTitle: '关掉这个 tab？',
    closeTabDetail: 'tab 里每个 pane 的进程都会结束。别的 tab 不受影响。',
    closePaneConfirm: '关闭 pane',
    composer: '写给这个 pane',
    send: '发送',
    sending: '正在发送…',
    sent: '已发送 ✓',
    sentLight: '已发送。它很短，屏幕上不好确认，看一眼 pane 吧。',
    notSubmitted: '没有提交 — %s',
    readOnly: '要在这里打字，打开「直接输入」，或者写在下面的框里。',
    streamClosed: 'Herdr 关掉了这个 pane 的流（%s）。',
    zoomFit: '适应宽度',
    zoomActual: '原始大小',
    zoomWrap: '换行显示',
    mirrorHint: '在换行显示、适应宽度和原始大小之间切换。',
    mirrorHintPhone: '在换行显示和适应宽度之间切换。',
    fullScreenNote: '全屏程序，按它的画面显示',
    keysHint: '显示或收起 Esc、Tab、方向键、^C 和数字键。',
    hintPhone: '双击画面，就能在换行显示和适应宽度之间切换。「按键」里有 Esc、方向键和 ^C。',
    modeObserve: '只看',
    modeTyping: '输入中',
    modeOffline: '离线',
    modeRetry: '重试 %s',
    hint: '在这里写，或者用下面的按键。「直接输入」会把你按的每个键都送进去，vim 这类全屏程序也一样。点一下画面可以收起键盘；双击画面，可以在适应宽度和原始大小之间切换。',
    dismiss: '关闭',
    keyNames: {escape: 'Esc', tab: 'Tab', shiftTab: 'Shift-Tab', enter: '回车', space: '空格', up: '上箭头',
      down: '下箭头', left: '左箭头', right: '右箭头', backspace: '删除', ctrlC: 'Control-C', ctrlD: 'Control-D'},
    choicesTitle: 'pane 在等你回答。',
    choicesHint: '把这个回答发给 pane，和在电脑上按下它一样。',
    choicesSelected: 'pane 上现在选中的是这一项。',
    choicesLasting: '这一项以后也算数，不只这一次。',
    choicesEscapeHint: '按 Esc，取消或退出这个问题。',
    choicesYes: '是',
    choicesNo: '否',
    choicesAnswered: '已经发给 pane，等它往下走。',
    choicesChanged: 'pane 上的问题刚刚变了，所以什么都没发。再看一眼再点。',
    composerHint: '输入框空着时，删除、回车和方向键直接发给 pane。',
  },
  agent: {
    default: '默认',
    composer: '跟 agent 说点什么…',
    composerShort: '说点什么…',
    send: '发送',
    steer: '补充',
    interrupt: '中断',
    dismissResult: '关闭结果',
    roleYou: '你',
    roleAgent: 'Agent',
    starting: '正在启动默认 agent…',
    continue: '接着用默认 agent。',
    thinking: '正在想…',
    reconnect: '重连',
    retry: '重试',
    details: '详情',
    answer: '回答',
    yourAnswer: '你的回答',
    accept: '同意',
    acceptSession: '本次会话都同意',
    decline: '拒绝',
    cancel: '取消',
    answered: '已回答',
    answeredOnHost: '%s（在电脑上）',
    kindCommand: '执行一条命令',
    kindFileChange: '改动文件',
    kindPermissions: '授予更多权限',
    kindUserInput: 'agent 需要你的输入',
    phaseStarting: '正在启动…',
    phaseReady: '就绪',
    phaseSending: '正在发送…',
    phaseWorking: '正在干活…',
    phaseStopping: '正在停止…',
    phaseReconnecting: '正在重连…',
    phaseFailed: '需要你处理',
    waitingOnYou: '在等你',
    needsInput: '需要你的输入',
    waitingCount: '%s 条待审批',
    context: '上下文',
    model: '模型',
    modelValue: '模型 %s，强度 %s',
    modelThread: '跟随线程',
    loadingModels: '正在读这个 agent 的模型…',
    modelsUnreadable: '读不到这个 agent 的模型。下一条沿用当前设置。',
    turnFailed: '这一轮失败了。',
    sendUnconfirmed: '这条没有确认送达。重新连接后再看这段对话。',
    previousSendUnconfirmed: '上一条还没确认送达。先确认再发别的。',
    attachmentGone: '有个附件已经不在电脑上了。移除后重新添加就好。',
    pathOnly: 'agent 没能直接收下图片，改为收到了文件路径。',
    answerUnconfirmed: '主机没有确认这个回答，agent 还在等。',
    steerTooLate: '这一轮在补充到达前就结束了。作为新消息发出去吧。',
    steerUnconfirmed: '补充没有确认。你的文字还在，检查这一轮后再发。',
    stopUnconfirmed: '没能确认停止。重新连接后看这一轮。',
    handoffKeep: '继续用现在的 agent',
    handoffDetail: '终端里的会话要交接一次才能用原生聊天。先看清改动，确认前什么都不动。',
    handoffReview: '查看这次改动',
    handoffConfirm: '确认交接',
    handoffLater: '以后再说',
    handoffAgain: '再看一次',
    handoffRollback: '如果连接失败',
    checkingSession: '正在检查当前会话…',
    connectingConversation: '正在接上同一段对话…',
    toolStatus: {running: '执行中', succeeded: '成功', failed: '失败', cancelled: '已取消', interrupted: '已停止', unknown: '未知'},
    toolTruncated: '更早的 %s 输出没有显示',
    // AGENT-COLLIE-1: steps, runs, change cards, questions, find (agent.step.* / run.* / change.* / question.* / find.*)
    stepRead: '读取',
    stepListed: '列出',
    stepSearched: '搜索',
    stepEdited: '编辑',
    stepCreated: '新建',
    stepDeleted: '删除',
    stepMoved: '移动',
    stepAndMore: '%s 和另外 %s 个',
    toolExit: '退出码 %s',
    runCommand: '1 条命令',
    runCommands: '%s 条命令',
    runRead: '读了 1 个文件',
    runReads: '读了 %s 个文件',
    runSearch: '1 次搜索',
    runSearches: '%s 次搜索',
    runEdit: '改了 1 个文件',
    runEdits: '改了 %s 个文件',
    runOther: '1 个其他步骤',
    runOthers: '%s 个其他步骤',
    runFailed: '%s 个失败',
    runEarlier: '前面还有 1 步',
    runEarlierMany: '前面还有 %s 步',
    runFold: '收起这些步骤',
    runExpandHint: '展开后能看到每一步。',
    changeMore: '再显示 %s 行',
    changeLess: '少显示一些',
    changeOmitted: '还有 %s 行没有发到手机上，在电脑上能看到。',
    changeFilesOmitted: '还改了 %s 个文件，在电脑上能看到。',
    changeWaiting: '等你允许这处改动',
    changeFile: '1 个文件',
    changeFiles: '%s 个文件',
    changeSee: '看这处改动',
    changeLinesLabel: '加了 %s 行，删了 %s 行',
    questionOther: '其他…',
    questionOtherPlaceholder: '用你自己的话回答',
    questionStep: '第 %s 题，共 %s 题',
    questionBack: '上一题',
    questionNext: '下一题',
    questionReview: '看一遍你的回答',
    questionHidden: '已隐藏',
    questionUnanswered: '还没回答',
    questionSelected: '已选',
    answeredWith: '已回答：%s',
    find: '查找',
    findPlaceholder: '在对话里查找',
    findCountLabel: '第 %s 处，共 %s 处',
    findNone: '没有找到',
    findPrevious: '上一处',
    findNext: '下一处',
    findClose: '关闭查找',
    jumpNewest: '跳到最新',
    jumpUnseen: '下面有新内容',
    runSep: '，',
    newConversation: '新对话',
    newBody: '这会和 agent 开始一段新对话。旧的那段还留在电脑上。',
    newStart: '开始新对话',
    newLater: '先不',
    newStarting: '正在开始新对话…',
    newWhileWorking: 'agent 正在跑这一轮。等它跑完或先停下，再开始新对话。',
    newWhileWaiting: 'agent 在等你回答。先回答它，再开始新对话。',
    newUnavailable: '对话还没连上。',
    planChip: '计划',
    planChipLabel: '计划模式已开启',
    planChipHint: '点一下回到默认模式。',
    planWhileWorking: '这一轮跑完后，才能切换计划模式。',
    planSwitching: '正在切换模式…',
    planFailed: '模式没有切换成功，再试一次。',
    planTitle: '拟定的计划',
    planImplement: '按这个计划动手',
    planImplementHint: '这会退出计划模式，让 agent 开始动手。',
    planKeep: '继续规划',
    planKeepHint: '留在计划模式，写下想改的地方。',
    planImplementMessage: '按计划开始实施。',
    diffTitle: '这段对话改过的文件',
    diffEmpty: '这段对话还没有改过文件。',
    diffOmitted: '更早的 %s 处改动没有显示。',
    diffClose: '关掉改动',
    queueCount: '%s 条在等',
    queueWhenEnds: '这一轮结束后就发出去。',
    queueWaiting: '已经停下了。准备好就发吧。',
    queueSendNow: '现在发',
    queueEdit: '编辑',
    queueDelete: '删除',
    queueSave: '保存',
    queueLabel: '在等的消息',
    queueEditGone: '它已经发出去了，你改的字放回了输入框。',
    queueUnconfirmed: '没有送到电脑上，再试一次。',
    stepsTitle: '步骤',
    stepsProgress: '%s/%s',
    stepsShow: '展开步骤',
    stepsHide: '收起步骤',
    stepDone: '已完成',
    stepNow: '进行中',
    stepTodo: '待做',
    declineWhy: '拒绝并说明原因…',
    declinePlaceholder: '为什么不行？一句话，可不填',
    earlier: '更早的消息',
    earlierLoading: '正在载入更早的消息…',
    trimmed: '还有 %s 个字符没在这里显示。',
  },
  attach: {
    button: '添加附件',
    photos: '照片',
    files: '文件',
    imageN: '图片 #%s',
    fileN: '文件 #%s',
    uploading: '正在上传 %s',
    remove: '移除 %s',
    dismiss: '关闭',
    markerGone: '文字里没有它的标记了，发送时会放在最前面。',
    tooLarge: '%s 超过 25 MB，太大了，发不了。',
    badType: '%s 发不了。能发的是图片、PDF 和文本或代码文件。',
    svg: 'SVG 不能发，因为它里面可能带脚本。换成 PNG 或 JPEG 就行。',
    notText: '%s 不是纯文本，不能当文本文件发。',
    heic: '电脑没能把这张照片转成 JPEG。',
    empty: '%s 是空的。',
    gone: '有个附件已经不在电脑上了。移除后重新添加就好。',
    failed: '%s 没传上去，再试一次。',
    tooMany: '一条消息最多带十个附件。',
    wait: '等上传完成再发送。',
    pathOnly: 'agent 没能直接收下图片，改为收到了文件路径。',
    picture: '附了一张图片',
    pictures: '附了 %s 张图片',
    drop: '松手即可添加',
  },
  links: {
    opening: '正在电脑上打开…',
    opened: '已在电脑上打开 %s。',
    openedAt: '已在电脑上打开 %s，停在第 %s 行。',
    watch: '在 Omodachi 的 Remote 里看',
    here: '已在新标签页打开 %s。',
    hereHint: '如果打不开，多半是这个服务只在电脑本机上监听，这里连不上。',
    onComputer: '在电脑上打开',
    dismiss: '关闭',
    image: '图片',
    refused: {
      open_not_mentioned: 'agent 显示的内容里已经没有它了，所以不打开。',
      open_outside_roots: '这个文件不在你的主目录或 agent 的目录里，所以不打开。',
      open_denied: '这是放密钥、密码或设置的私密位置，不从这里打开。',
      open_executable: '这是程序或脚本，不会从这里运行；真要打开，请在电脑上操作。',
      open_missing: '电脑上没有这个文件（或已经不在了）。',
      open_source_gone: '那条消息或那个窗格已经不在了，没法核对这个链接。',
      open_type_refused: '它不是文件也不是文件夹，没什么可打开的。',
      open_url_refused: '这种方式只能打开网址。',
      open_rate_limited: '一分钟里打开得太多了，稍等一下再试。',
      open_unavailable: '电脑现在打不开它。电脑屏幕上有人登录着吗？',
      web_client_forbidden: '这个网页不能在电脑上打开东西。',
      host_unreachable: '电脑没有回应，稍后再试。',
    },
    refusedUnknown: '电脑没有打开它（%s）。',
  },
  reasons: {
    herdrUnknown: '主机未能完成这次 Herdr 请求（%s）。',
    agentUnknown: '主机未能完成这次 agent 请求（%s）。',
    herdr: {
      herdr_control_in_use: '另一端正在控制这个 pane。当前是只读观察。',
      herdr_unavailable: '主机的 Herdr 会话没有应答。',
      herdr_request_failed: 'Herdr 拒绝了这次操作。',
      invalid_pane: '这个 pane 已经不在主机的布局里了。',
      invalid_workspace: '这个 workspace 已经不在主机的布局里了。',
      invalid_geometry: '终端尺寸超出 Herdr 能接受的范围。',
      herdr_action_unsupported: '主机的 Herdr 桥不支持这个动作。',
      invalid_key: 'Herdr 不认识这个键。',
      herdr_key_unsupported: 'Herdr 发不了 PageUp、PageDown、Home、End、Insert 和 Delete。',
      herdr_too_many_keys: '一次的按键太多了。',
      invalid_text: '文字里有终端控制字符，没有发送。',
      herdr_text_too_long: '文字超过 16 KB，没有发送。',
      herdr_pane_gone: 'pane 已经不在电脑上了。',
      not_seen: '文字没有出现在 pane 里，所以没有按回车。可能有个对话框拿着焦点。',
      submit_failed: '文字送到了，回车没有按成功。',
      not_requested: '文字送到了，没有按回车。',
      herdr_agent_blocked: 'agent 正停在一个问题上，所以一个字都没打进去；用输入框上面的选项回答它，或者用按键（1、↓ ↩ 或 esc）。',
      invalid_session: '电脑上已经没有这个会话了。',
      invalid_session_name: '名字只能用字母、数字和 . _ -，以字母或数字开头，最多 32 个字符。',
      herdr_session_exists: '电脑上已经有一个叫这个名字的会话了。换个名字，或者在列表里选它。',
      herdr_session_start_failed: '会话没能在电脑上启动起来。过一会儿再试一次。',
      invalid_folder: '这看起来不像一个文件夹。试试 ~/Work/project 这样的路径。',
      folder_outside_home: '请选电脑上你主目录里面的文件夹。',
      folder_private: '这个文件夹里放着密钥或私密设置，Omodachi 不会在这里开工作区。',
      folder_not_found: '电脑上没有这个文件夹。检查一下路径，或者从列表里选一个。',
      folder_unreadable: '这个文件夹打不开，没法看里面有什么。',
      invalid_workspace_name: '名字只能一行，最多 64 个字符。',
      herdr_launch_not_allowed: '电脑上已经没有这个启动项了。关掉卡片再打开一次看看。',
      host_unreachable: '主机没有回应。',
    },
    agent: {
      existing_thread_handoff_required: '现有的 agent 需要做一次连接交接。',
      agent_busy: 'agent 正在干活，任务保留着。空了再试。',
      agent_pane_busy: 'agent 正在干活，任务保留着。空了再试。',
      handoff_rolled_back: '连接没能改动，原来的终端和对话已复原。',
      handoff_plan_expired: '会话变了或方案过期。看一份新的再继续。',
      handoff_target_changed: '会话变了或方案过期。看一份新的再继续。',
      handoff_stop_unconfirmed: '交接结果未确认。先检查现有会话再重试。',
      handoff_attach_unconfirmed: '交接结果未确认。先检查现有会话再重试。',
      handoff_thread_unconfirmed: '交接结果未确认。先检查现有会话再重试。',
      handoff_transport_unavailable: '这次交接需要的通道还没连上。',
      agent_owner_start_failed: '主机上的 agent 没能启动。',
      agent_owner_start_timeout: '主机上的 agent 没能启动。',
      agent_owner_socket_unavailable: '主机上的 agent 没能启动。',
      thread_creation_unconfirmed: '主机上的 agent 没能启动。',
      provider_request_rejected: '主机上的 agent 没能启动。',
      structured_agent_kind_unsupported: '主机上的 agent 没能启动。',
      agent_state_unknown: '主机上的 agent 没能启动。',
      agent_not_working: 'agent 现在没有在跑任务。',
      agent_chat_unavailable: '这台主机没有提供 agent 对话。',
      agent_management_unavailable: '这台主机没有提供 agent 对话。',
      agent_approval_unknown: '主机不认识这条审批。',
      agent_approval_decision_invalid: '主机不接受这个审批选项。',
      default_agent_unset: '先在电脑的 Omarchy 菜单里选一个默认 agent。',
      history_unavailable: '读不到这段对话的历史。',
      cancellation_pending: '主机还在停这一轮，稍后再试。',
      turn_failed: '这一轮失败了。',
      host_unreachable: '主机没有回应。',
      agent_turn_active: 'agent 正在跑这一轮。等它跑完或先停下，再开始新对话。',
      agent_approval_pending: 'agent 在等你回答。先回答它，再开始新对话。',
      agent_identity_changed: '这段对话已经换成了新的一段，现在显示新的。',
      agent_conversation_changing: '正在开始新对话，稍等再试。',
      new_conversation_confirmation_required: '开始新对话前要先确认。',
      agent_thread_creation_unconfirmed: '新对话还没确认好，再试一次。',
      agent_mode_unavailable: '电脑上的 Codex 现在切换不了计划模式。',
      agent_command_while_working: '等 agent 跑完这一轮，才能用这个。',
      invalid_agent_mode: '这里没有这个模式。',
    },
  },
  pair: {
    title: '配对这个浏览器',
    lead: `${APP_NAME} 是 %s 自己的页面。电脑前的人批准这个浏览器之后才能用。`,
    start: '请求配对',
    asking: '正在请求…',
    waitingTitle: '等待批准',
    waitingBody: '在电脑上打开 Omodachi 面板，在写着这个浏览器的卡片上点 Approve。',
    cardSays: '卡片上写着',
    device: '设备',
    via: '经由',
    viaTailscale: 'Tailscale',
    expires: '这个请求在 %s 失效。',
    cancel: '取消', //                               action.cancel
    rejectedTitle: '没有被批准',
    rejectedBody: '请求在电脑上被拒绝了，或者已经超时。可以重新请求。',
    revokedTitle: '这个浏览器已经不再配对',
    revokedBody: '它的权限在电脑上被移除了，或者已经过期。这里什么都没留下。重新配对就能回来。',
    expiredTitle: '这个浏览器的配对过期了',
    again: '重新配对',
    failed: '电脑没有回应这个请求。',
    grants: '批准后，这个浏览器能用 Omarchy 菜单、快捷键、通知、Herdr 和 agent。永远没有屏幕，也没有 SSH。',
    insecure: '这个页面不在安全地址上，没法配对。请用电脑的 https://… .ts.net 地址打开。',
  },
  settings: {
    host: '主机', //                                gate.sshHostKey.host
    connected: '已连接', //                         settings.connected
    connecting: '正在连接',
    offline: '离线',
    address: '地址',
    browser: '这个浏览器',
    label: '在电脑上显示为',
    deviceId: '设备',
    expires: '凭据到期',
    renews: '在最后一周里，只要用这个页面，它会自己续期。',
    forget: '在这里忘掉配对',
    forgetConfirm: '确认忘掉',
    forgetNote: '这只清除这个浏览器里的东西。要收回它的权限，请在电脑的 Devices 页面移除它。',
    language: '语言',
    languageAuto: '自动',
    about: '关于', //                               settings.about
    tourGroup: '帮助',
    tourReplay: '再看一遍导览',
    tourReplayDetail: '沿着 bar 走一圈。Herdr 和 Agent 里的小提示也会再出现。',
    build: '构建', //                               settings.build
    contract: '契约', //                            settings.contract
    app: 'OMODACHI APP',
    appOpen: '打开 App',
    appGet: '获取 App',
    odomo: `${APP_NAME} 由电脑自己的 Omodachi 提供，只在这个地址上。它只在这个浏览器里存一份凭据，别的什么都不存。`,
    road: '怎么连到它',
    roadTailscale: '经 Tailscale，用名字 %s。在家在外都一样。',
    roadOther: '地址 %s。',
    barEntries: 'BAR 上的入口',
    barEntriesNote: '关掉一项，它的入口就从这条 bar 上消失。',
    barEntryLabel: '%s 的 bar 入口',
    barEntryLocked: '设置自己不能关掉',
    showUnrunnable: '显示无法在此执行的绑定',
    showUnrunnableDetail: '主机把这几条快捷键绑在 Lua 函数上，没有可执行的记录，这台设备永远跑不了。打开后它们回到列表里，仍然是灰的。',
    licence: `许可证 · MIT`,
    licenceDetail: `${APP_NAME} 和它用到的组件`,
    licenceBody: `${APP_NAME}：MIT，和 omodachi-core 其余部分一样。用到了 React（MIT）、xterm.js 的终端核心 @xterm/headless（MIT，© The xterm.js authors）和 JetBrains Mono（SIL OFL 1.1）。它们的许可证全文和 ${APP_NAME} 放在一起。`,
    sourceCode: '源代码',
    privacy: '隐私',
  },
  push: {
    group: '这台设备上的通知',
    on: '开启通知',
    off: '关闭通知',
    turningOn: '正在开启…',
    stateOn: '已开启',
    stateOff: '已关闭',
    note: '通知由电脑自己发出，只为这个浏览器加密，经它的推送服务（Apple、Google 或 Mozilla）送达。只说哪里需要你，不多说。',
    hostOff: '已在电脑的 Devices 页面关掉。',
    addToHome: `在 iPhone 和 iPad 上，通知只在主屏幕 App 里有：分享 › 添加到主屏幕，再从那里打开 ${APP_NAME}。`,
    denied: '这个浏览器禁止了这个页面的通知。请在浏览器设置里允许后再回来。',
    unsupported: '这个浏览器收不到推送通知。',
    insecure: '通知需要电脑的 https://… .ts.net 地址。',
    failed: '没能开启通知（%s）。',
    kinds: '要推送什么',
    agent_approval: 'Agent 需要你批准',
    agent_needs_input: 'Agent 在等你回复',
    agent_done: 'Agent 完成了',
    herdr_blocked: 'Herdr 里有 agent 在等你',
    host_notification: '电脑上的通知',
    herdr_done: 'Herdr 里有 agent 做完了，你还没看',
    snooze: '暂停',
    snoozeOff: '不暂停',
    snooze1: '1 小时',
    snooze8: '8 小时',
    snoozedUntil: '%s 之前不推送。',
    quiet: 'App 在前台时不重复推送',
    quietDetail: 'Omodachi App 开在你面前、并且自己就会提醒同一件事时，这里不再重复推送。App 不提醒的事，比如 agent 完成了一轮，照样推到这里。',
    desk: '人在电脑前也提醒我',
    deskDetail: '关着时：你正在用电脑，提醒就先等着。电脑闲置两分钟或锁屏后，它们才推到这里，你在电脑上已经处理过的不会再推。Herdr 的提醒总会先等 30 秒，好几个 pane 在等时合成一条。',
  },
  install: {
    title: `把 ${APP_NAME} 放到主屏幕上`,
    lead: `${APP_NAME} 是 %s 自己的页面。在你的 %s 上，它是主屏幕上的一个 App，也在那里配对。`,
    whyTitle: '为什么值得',
    whyNotify: '即使 %s 锁着屏、Omodachi App 也没开，通知照样能送到你手上。',
    whyOpen: '从主屏幕一点，就全屏打开，和 App 一样。',
    whyStays: '它会一直登录着这台电脑：配对一次，它就记住了。',
    whyStore: '不用去商店下载任何东西，它就来自你自己的电脑。',
    howTitle: '三步',
    step1: '点「分享」',
    step1Detail: '在 Safari 里，它在地址栏旁边：有时就是一个按钮，有时收在旁边的菜单里。在 Omodachi App 的浏览器视图里，就是工具栏上的「分享」按钮。',
    step2: '选「添加到主屏幕」',
    step2Detail: '列表里没有的话，点「查看更多」就能看到。',
    step3: `从主屏幕打开 ${APP_NAME}`,
    step3Detail: '在那里配对。电脑前的人批准一次，就都好了。',
    whyFirst: '配对只在主屏幕 App 里进行。它在你的 %s 上有自己的一份存储，在浏览器里配好的不会跟过去。',
    oldPairing: '这个浏览器以前配对过。那份配对这里不再使用了，可以在电脑的 Devices 页面把它移除。',
    otherApp: '如果是在别的 App 里打开的，找不到「分享」，先用 Safari 打开这个页面。',
    offerTitle: `${APP_NAME} 也可以放到主屏幕上`,
    offerAndroid: '放上去以后一点就全屏打开，和 App 一样。通知在浏览器里也能收到，所以装不装都随你。',
    offerDesktop: `通知在这个标签页里就能收到。愿意的话，${APP_NAME} 也可以有自己的窗口。`,
    androidSteps: `在 Chrome 里打开 ⋮ 菜单，选「添加到主屏幕」（或「安装应用」），再从那里打开 ${APP_NAME}。配对会一起带过去。`,
    installButton: `安装 ${APP_NAME}`,
    group: '安装',
    stateInstalled: '已安装',
    stateBrowser: '在浏览器里',
    installedNote: `${APP_NAME} 已经装在这台设备上，会像 App 一样单独打开。`,
    installedNow: `${APP_NAME} 装好了，可以从主屏幕或应用列表里打开。`,
  },
  errors: {
    generic: '主机返回了 %s。',
    offline: '主机没有回应。',
  },
  tour: {
    label: '导览',
    next: '下一步',
    skip: '跳过',
    done: '完成',
    progress: '第 %s 步，共 %s 步',
    steps: {
      bar: {title: '你的 bar',
        body: '每个面板都在这条 bar 上，一点就到；点 logo 是电脑的 Omarchy 菜单。按住图标，或者把指针停在上面，能看到它的名字。'},
      remote: {title: 'Remote',
        body: 'Remote 在 iPhone 或 iPad 上的 Omodachi app 里打开：电脑的屏幕，可以当又一块屏，也可以接管整个桌面。'},
      agent: {title: 'Agent',
        body: '和电脑上的 agent 聊天，也能发照片或文件给它。它要运行命令或改文件之前，会先在这里问你。'},
      herdr: {title: 'Herdr',
        body: '看着 agent 们在各自的 pane 里干活，也能往任何一个里打字。「快捷」一点就发出常用的回复。'},
      notifications: {title: '通知',
        body: `电脑上的通知都在这一张列表里。在设置里开启通知，agent 在等你时 ${APP_NAME} 会告诉你。`},
      settings: {title: '设置',
        body: '这台电脑、bar 和语言都在这里。这段导览也在，就在靠上的「再看一遍导览」。'},
    },
  },
  overview: {
    title: '谁在等你',
    open: '看看谁在等你',
    openHint: '列出 Agent 和每个会话里在等你、在做和做完的 agent。',
    empty: '现在没有等你的事。',
    close: '收起',
    sections: {needs_you: '在等你', working: '在做', done: '做完了，还没看'},
    count: {needs_you: '等你 %s', working: '在做 %s', done: '做完 %s'},
    ask: {
      approve_command: '批准一条命令',
      approve_files: '批准文件改动',
      approve_permissions: '批准更多权限',
      approve: '批准一个请求',
      answer: '回答一个问题',
      pick: '选一个选项',
      pickQuestion: '选一个：%s',
      yes_no: '回答是或否',
      yesNoQuestion: '是或否：%s',
      working: '正在做',
      yourMove: '该你了：%s',
      finished: '做完了',
      failed: '出错停下了',
    },
    agent: 'Agent',
    needsYouBadge: '%s 个在等你',
    chip: {needs_you: '有在等你的', working: '有在做的', done: '有做完还没看的'},
  },
  face: {
    label: 'Agent：%s',
    states: {idle: '空闲', thinking: '在思考', working: '在干活', tool: '在跑命令',
      waiting: '在等你', done: '完成了', error: '出错停下了', offline: '离线',
      stopped: '已停止'},
  },
};

export const LANGUAGES = {en, 'zh-Hans': zh};

// navigator.languages, in order; the first Chinese or English one wins, and
// anything else is English. `override` is the Settings choice.
export function pickLanguage(languages, override) {
  if (override && LANGUAGES[override]) return override;
  for (const value of languages || []) {
    const tag = String(value).toLowerCase();
    if (tag.startsWith('zh')) return 'zh-Hans';
    if (tag.startsWith('en')) return 'en';
  }
  return 'en';
}

export function fill(template, ...values) {
  let out = template;
  values.forEach((v) => {
    out = out.replace('%s', String(v));
  });
  return out;
}
