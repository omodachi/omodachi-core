import React from 'react';
import Icon from '../Icon.jsx';
import {PanelArea, GroupLabel, ValueLine, SettingChoice, SettingAction, StateChip, InlineConfirm, SquareToggleRow} from '../ds.jsx';
import {fill} from '../lib/i18n.js';
import {PANELS, LOCKED} from '../lib/panels.js';
import {useCopy} from '../copy.js';
import PushSettings from './PushSettings.jsx';
import {appLinks, getAppLink} from '../lib/applink.js';
import {InstallOffer, usePrompt} from '../Install.jsx';

// Settings, Odomo's own: which host this is, what this browser is to it, the
// language, and what the page is. Shaped like omodachi-web's SettingsPanel
// (main 0da28c9) - a read-and-choose column of the same primitives.

function day(seconds) {
  if (!Number.isInteger(seconds)) return '-';
  return new Date(seconds * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

// ODOMO-2: the source and the privacy page, as the App's About has them.
export const SOURCE_URL = 'https://github.com/omodachi/omodachi-core';
export const PRIVACY_URL = 'https://omodachi.app/privacy';

const GLYPH = {remote: 'display', agent: 'robot', herdr: 'stack', settings: 'gear', notifications: 'bell'};

export default function SettingsPanel({client, state, status, credential, build, hostId, language, onLanguage, onForget,
  install, barOff = [], onBar = () => {}, onTour = () => {}, showUnrunnable = false, onShowUnrunnable = () => {}}) {
  const [licence, setLicence] = React.useState(false);
  const {t} = useCopy();
  const S = t.settings;
  const I = t.install;
  const prompt = usePrompt();
  const isInstalled = !!(install && install.installed) || prompt.installedNow;
  const name = (state && state.host && state.host.name) || credential.host_name || location.hostname.split('.')[0];
  const chip = status === 'connected' ? [S.connected, 'ok'] : status === 'offline' ? [S.offline, 'muted'] : [S.connecting, 'muted'];
  return (
    <PanelArea title={t.panels.settings} testid="panel-settings" label={t.panels.settings}>
      <GroupLabel>{S.host}</GroupLabel>
      <div className="hostrow">
        <Icon name="display" size={18} />
        <span className="hostrow__tx">
          <b data-testid="settings-host">{name}</b>
          <span>{location.host}</span>
        </span>
        <StateChip text={chip[0]} kind={chip[1]} />
      </div>
      {/* TAIL-3b's one road, as the App says it under the host's name. */}
      <p className="snote" data-testid="settings-road">{/\.ts\.net$/.test(location.hostname)
        ? fill(S.roadTailscale, location.hostname.split('.')[0]) : fill(S.roadOther, location.hostname)}</p>
      <div className="frule" />

      {/* TUTORIAL-1: the tour again, with every tip (one registry, lib/hints.js). */}
      <GroupLabel>{S.tourGroup}</GroupLabel>
      <SettingAction title={S.tourReplay} detail={S.tourReplayDetail} testid="settings-tour-replay" onClick={onTour} />
      <div className="frule" />

      <PushSettings client={client} />
      <div className="frule" />

      {/* LITE-1e: whether Odomo is installed here, and on Android / desktop
          the offer to install it. (An iPhone or iPad only gets this far in
          the Home Screen app, so there it always says Installed.) */}
      <GroupLabel action={<StateChip text={isInstalled ? I.stateInstalled : I.stateBrowser} kind={isInstalled ? 'ok' : 'muted'} />}>
        {I.group}
      </GroupLabel>
      <div data-testid="settings-install" data-installed={isInstalled ? '1' : '0'}>
        {isInstalled ? (
          <p className="snote">{prompt.installedNow && !(install && install.installed) ? I.installedNow : I.installedNote}</p>
        ) : install && install.platform === 'android' ? (
          <InstallOffer platform="android" testid="settings-install-offer" />
        ) : prompt.available ? (
          <InstallOffer platform="desktop" testid="settings-install-offer" />
        ) : (
          <p className="snote">{I.offerDesktop}</p>
        )}
      </div>
      <div className="frule" />

      {/* LITE-1c: the way to the Omodachi app (this computer, in the app), and
          the omodachi.app page that says where to get it. */}
      <GroupLabel>{S.app}</GroupLabel>
      <div className="setting">
        <div className="setting__c sapp">
          <a className="fbtn fbtn--primary" href={appLinks('host', hostId).universal} target="_blank"
            rel="noopener noreferrer" data-testid="settings-app-open">{S.appOpen}</a>
          <a className="fbtn" href={getAppLink(hostId)} target="_blank" rel="noopener noreferrer"
            data-testid="settings-app-get">{S.appGet}</a>
        </div>
      </div>
      <div className="frule" />

      {/* N-38: the bar's switches. Settings cannot switch itself off. */}
      <GroupLabel>{S.barEntries}</GroupLabel>
      <p className="snote">{S.barEntriesNote}</p>
      <div data-testid="settings-bar-entries">
        {PANELS.map((id) => {
          const locked = LOCKED.includes(id);
          return (
            <SquareToggleRow key={id} rowTestid={'settings-entry-' + id} glyph={GLYPH[id]} title={t.panels[id]}
              detail={locked ? S.barEntryLocked : null} on={locked || !barOff.includes(id)}
              label={fill(S.barEntryLabel, t.panels[id])} disabled={locked} testid={'settings-entry-' + id + '-toggle'}
              onChange={(on) => onBar(id, on)} />
          );
        })}
      </div>
      <div className="frule" />

      {/* CLIP-1 §1: the rows the host says cannot run here. */}
      <GroupLabel>{t.panels.keybindings}</GroupLabel>
      <SquareToggleRow title={S.showUnrunnable} detail={S.showUnrunnableDetail} on={showUnrunnable}
        onChange={onShowUnrunnable} testid="settings-show-unrunnable" />
      <div className="frule" />

      <GroupLabel>{S.browser}</GroupLabel>
      <ValueLine name={S.label} value={credential.client_label || '-'} />
      <ValueLine name={S.deviceId} value={credential.device_id || '-'} testid="settings-device" arrange="under" />
      <ValueLine name={S.expires} value={day(credential.expires_at)} />
      <p className="snote">{S.renews}</p>
      <div className="setting">
        <div className="setting__c">
          <InlineConfirm title={S.forget} confirmTitle={S.forgetConfirm} kind="ghost" testid="settings-forget" onConfirm={onForget} />
        </div>
      </div>
      <p className="snote">{S.forgetNote}</p>
      <div className="frule" />

      {/* ODOMO-SETTINGS-1: a titled row, beside its title when it fits. */}
      <SettingChoice title={S.language} testid="settings-language" rowTestid="settings-language-row"
        value={language || null} onChange={onLanguage}
        options={[[S.languageAuto, null], ['English', 'en'], ['简体中文', 'zh-Hans']]} />
      <div className="frule" />

      <GroupLabel>{S.about}</GroupLabel>
      <ValueLine name={S.build} value={build || '-'} testid="settings-build" />
      <ValueLine name={S.contract} value="omodachi.v1" />
      <SettingAction title={S.licence} detail={S.licenceDetail} trailing={licence ? 'up' : 'down'} aria-expanded={licence}
        testid="settings-licence" onClick={() => setLicence(!licence)} />
      {licence ? <p className="snote" data-testid="settings-licence-body">{S.licenceBody}</p> : null}
      <SettingAction title={S.sourceCode} detail={SOURCE_URL} href={SOURCE_URL} target="_blank" rel="noopener noreferrer"
        testid="settings-source-code" />
      <SettingAction title={S.privacy} detail={PRIVACY_URL} href={PRIVACY_URL} target="_blank" rel="noopener noreferrer"
        testid="settings-privacy" />
      <p className="snote">{S.odomo}</p>
    </PanelArea>
  );
}
