import React, {useCallback, useEffect, useState} from 'react';
import {GroupLabel, FlowButton, SettingChoice, SquareToggleRow, StateChip} from '../ds.jsx';
import {useCopy} from '../copy.js';
import {fill} from '../lib/i18n.js';
import {KINDS, OFF_BY_DEFAULT, prepare, snoozeUntil, snoozed, support, turnOff, turnOn} from '../lib/push.js';
import {InstallSteps} from '../Install.jsx';

// LITE-1b L3: Settings › Notifications on this device. One switch that
// subscribes inside the tap, then what to send, a snooze, and "quiet while the
// app is open" - all stored on the host (`/v1/notifications/prefs`), so the
// host decides before it sends anything. When the browser cannot (an iPhone
// Safari tab, a blocked permission) it says so instead of offering a switch.

function clock(seconds, lang) {
  try {
    return new Date(seconds * 1000).toLocaleTimeString(lang === 'zh-Hans' ? 'zh-CN' : 'en-GB', {hour: '2-digit', minute: '2-digit'});
  } catch {
    return '';
  }
}

export default function PushSettings({client}) {
  const {t, lang} = useCopy();
  const P = t.push;
  const [can] = useState(() => support(window));
  const [prepared, setPrepared] = useState(null);
  const [prefs, setPrefs] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const reload = useCallback(() => client.prefs().then(setPrefs).catch(() => {}), [client]);
  useEffect(() => {
    reload();
    if (can.state !== 'ready') return;
    prepare(client).then(setPrepared).catch((failure) => setError(failure && (failure.code || failure.message)));
  }, [client, can.state, reload]);

  // The host writes this device's push titles in the language Odomo is in.
  useEffect(() => {
    if (prefs && prefs.lang !== lang) {
      client.setPrefs(prefs.revision, {lang}).then(setPrefs).catch(() => {});
    }
  }, [client, prefs, lang]);

  const change = (changes) => {
    if (!prefs) return;
    client.setPrefs(prefs.revision, changes).then(setPrefs).catch((failure) => {
      if (failure && failure.code === 'stale_revision') reload();
    });
  };

  // The tap. `turnOn` calls PushManager.subscribe before it awaits anything.
  const on = () => {
    if (!prepared) return;
    setBusy(true);
    setError(null);
    turnOn(client, prepared, {lang})
      .then(() => (prefs && prefs.lang !== lang ? client.setPrefs(prefs.revision, {lang}) : client.prefs()))
      .then(setPrefs)
      .catch((failure) => setError((failure && (failure.code || failure.name || failure.message)) || 'error'))
      .finally(() => setBusy(false));
  };
  const off = () => {
    setBusy(true);
    turnOff(client, prepared).then(reload).catch(() => {}).finally(() => setBusy(false));
  };

  const subscribed = !!(prefs && prefs.subscribed);
  const blocked = {add_to_home_screen: P.addToHome, denied: P.denied, unsupported: P.unsupported, insecure: P.insecure}[can.state];
  const hours = prefs && snoozed(prefs) ? null : 0;
  return (
    <div data-testid="push-settings" data-subscribed={subscribed ? '1' : '0'}>
      <GroupLabel action={<StateChip text={subscribed ? P.stateOn : P.stateOff} kind={subscribed ? 'ok' : 'muted'} />}>
        {P.group}
      </GroupLabel>
      {blocked ? <p className="snote" data-testid="push-blocked">{blocked}</p> : null}
      {/* LITE-1e: the same three steps as the install screen. */}
      {can.state === 'add_to_home_screen' ? <InstallSteps testid="push-install-steps" /> : null}
      {blocked ? null : (
        <div className="setting">
          <div className="setting__c">
            {subscribed ? (
              <FlowButton kind="secondary" onClick={off} disabled={busy} data-testid="push-off">{P.off}</FlowButton>
            ) : (
              <FlowButton kind="primary" onClick={on} disabled={busy || !prepared} data-testid="push-on">
                {busy ? P.turningOn : P.on}
              </FlowButton>
            )}
          </div>
        </div>
      )}
      {error ? <p className="snote snote--err" role="alert" data-testid="push-error">{fill(P.failed, error)}</p> : null}
      {prefs && !prefs.host_enabled ? <p className="snote" data-testid="push-host-off">{P.hostOff}</p> : null}
      <p className="snote">{P.note}</p>
      {prefs ? (
        <>
          <GroupLabel>{P.kinds}</GroupLabel>
          {KINDS.map((kind) => (
            <SquareToggleRow key={kind} title={P[kind]}
              on={OFF_BY_DEFAULT.includes(kind) ? prefs.kinds[kind] === true : prefs.kinds[kind] !== false}
              testid={'push-kind-' + kind}
              onChange={(value) => change({kinds: {[kind]: value}})} />
          ))}
          <SquareToggleRow title={P.quiet} detail={P.quietDetail} on={prefs.quiet_when_native_connected !== false}
            testid="push-quiet" onChange={(value) => change({quiet_when_native_connected: value})} />
          {/* NOTIFY-1: the switch says "alert me anyway", so on = not quiet. */}
          <SquareToggleRow title={P.desk} detail={P.deskDetail} on={prefs.quiet_at_desk === false}
            testid="push-desk" onChange={(value) => change({quiet_at_desk: !value})} />
          {/* ODOMO-SETTINGS-1: a titled row, beside its title when it fits. */}
          <SettingChoice title={P.snooze} testid="push-snooze" rowTestid="push-snooze-row" value={hours}
            onChange={(value) => change({snooze_until: snoozeUntil(value)})}
            options={[[P.snoozeOff, 0], [P.snooze1, 1], [P.snooze8, 8]]} />
          {snoozed(prefs) ? <p className="snote" data-testid="push-snoozed">{fill(P.snoozedUntil, clock(prefs.snooze_until, lang))}</p> : null}
        </>
      ) : null}
    </div>
  );
}
