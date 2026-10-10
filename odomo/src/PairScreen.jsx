import React, {useEffect, useRef, useState} from 'react';
import {useCopy} from './copy.js';
import {Symbol} from './Icon.jsx';
import {FlowButton} from './ds.jsx';
import {fill} from './lib/i18n.js';
import {HostClient} from './lib/api.js';
import {browserLabel, newDeviceId} from './lib/device.js';
import {POLL_MS, pollClaim, requestPairing} from './lib/pairing.js';
import * as storage from './lib/storage.js';
import {InstallOffer} from './Install.jsx';

// L2. Request → waiting (with what the card on the computer shows) → claim.
// The request survives a reload; the device id survives everything but
// "forget", so asking again replaces this browser's own request instead of
// stacking a second card.

function hostLabel() {
  const name = location.hostname || '';
  return /^[0-9.:[\]]+$/.test(name) ? name : name.split('.')[0] || name;
}

function clock(seconds, lang) {
  try {
    return new Date(seconds * 1000).toLocaleTimeString(lang === 'zh-Hans' ? 'zh-CN' : 'en-GB', {hour: '2-digit', minute: '2-digit'});
  } catch {
    return '';
  }
}

export default function PairScreen({base, lost, onPaired, install}) {
  const {t, lang} = useCopy();
  const client = useRef(new HostClient({base})).current;
  const [pending, setPending] = useState(null);
  const [stage, setStage] = useState(lost && lost !== 'forgotten' ? 'lost' : 'start');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const label = browserLabel(navigator.userAgent, navigator.maxTouchPoints || 0);

  useEffect(() => {
    storage.get('pending').then((value) => {
      if (value && value.request_id && value.request_secret) {
        setPending(value);
        setStage('waiting');
      }
    });
  }, []);

  // Poll the claim while waiting. A dead request (rejected, expired, already
  // used) ends the wait; a network error just tries again.
  useEffect(() => {
    if (stage !== 'waiting' || !pending) return undefined;
    let live = true;
    let timer = 0;
    const tick = async () => {
      const result = await pollClaim(client, pending);
      if (!live) return;
      if (result.state === 'claimed') {
        await storage.del('pending');
        onPaired(result.credential);
        return;
      }
      if (result.state === 'dead') {
        await storage.del('pending');
        setPending(null);
        setStage('rejected');
        return;
      }
      timer = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [stage, pending, client, onPaired]);

  const ask = async () => {
    setBusy(true);
    setError(null);
    try {
      let device = await storage.get('device');
      if (!device || typeof device.device_id !== 'string') {
        device = {device_id: newDeviceId()};
        await storage.set('device', device);
      }
      const request = await requestPairing(client, {deviceId: device.device_id, label});
      await storage.set('pending', request);
      setPending(request);
      setStage('waiting');
    } catch (failure) {
      setError(failure && failure.status ? fill(t.errors.generic, failure.code) : t.pair.failed);
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    await storage.del('pending');
    setPending(null);
    setStage('start');
  };

  const secure = window.isSecureContext;
  return (
    <div className="pair" data-testid="pair" data-stage={stage}>
      <div className="pair__col">
        <span className="pair__mark"><Symbol size={44} /></span>
        {stage === 'waiting' && pending ? (
          <>
            <h1 className="pair__title"><span className="pair__wait" aria-hidden="true" />{t.pair.waitingTitle}</h1>
            <p className="pair__lead">{t.pair.waitingBody}</p>
            <div className="pair__card" data-testid="pair-card">
              <div className="pair__kv"><span>{t.pair.cardSays}</span><b className="pair__code">{pending.client_label || label}</b></div>
              <div className="pair__kv"><span>{t.pair.device}</span><b data-testid="pair-device">{pending.device_id}</b></div>
              {/* LITE-1b (NEXT-1 §6.3): never the forwarded 100.x - the card
                  on the computer names the Tailscale device, not its address. */}
              {pending.via && pending.via.kind === 'tailnet' ? (
                <div className="pair__kv"><span>{t.pair.via}</span><b>{t.pair.viaTailscale}</b></div>
              ) : null}
            </div>
            {pending.expires_at ? <p className="pair__note">{fill(t.pair.expires, clock(pending.expires_at, lang))}</p> : null}
            <p className="pair__note">{t.pair.grants}</p>
            <div className="pair__act">
              <FlowButton kind="secondary" onClick={cancel} data-testid="pair-cancel">{t.pair.cancel}</FlowButton>
            </div>
          </>
        ) : (
          <>
            {stage === 'lost' ? (
              <div className="pair__card bad" data-testid="pair-lost" data-reason={lost}>
                <h1 className="pair__title">{lost === 'credential_expired' ? t.pair.expiredTitle : t.pair.revokedTitle}</h1>
                <p className="pair__lead">{t.pair.revokedBody}</p>
              </div>
            ) : stage === 'rejected' ? (
              <div className="pair__card warn" data-testid="pair-rejected">
                <h1 className="pair__title">{t.pair.rejectedTitle}</h1>
                <p className="pair__lead">{t.pair.rejectedBody}</p>
              </div>
            ) : (
              <h1 className="pair__title">{t.pair.title}</h1>
            )}
            <p className="pair__lead">{fill(t.pair.lead, hostLabel())}</p>
            <p className="pair__note">{t.pair.grants}</p>
            {secure ? null : <p className="pair__err">{t.pair.insecure}</p>}
            {error ? <p className="pair__err" role="alert">{error}</p> : null}
            <div className="pair__act">
              <FlowButton kind="primary" onClick={ask} disabled={busy || !secure} data-testid="pair-start">
                {busy ? t.pair.asking : stage === 'start' ? t.pair.start : t.pair.again}
              </FlowButton>
            </div>
            {/* LITE-1e: Android and desktop pair right here; installing is only offered. */}
            {install && install.gate === 'offer' ? <InstallOffer platform={install.platform} compact /> : null}
          </>
        )}
      </div>
    </div>
  );
}
