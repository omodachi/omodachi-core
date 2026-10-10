// Pairing a browser, as a sequence the page can resume after a reload.
//
//   request  POST /v1/pairing/requests {device_id, device_name, client}
//            - the browser sends `Origin`, so core makes it a `web` client
//   wait     the person at the computer approves on the Omodachi panel
//   claim    POST …/claim {request_secret} until it holds a credential
//
// The request (id, secret, expiry) is kept until it is claimed or dead, so a
// reload in the middle keeps waiting on the same card instead of making a
// second one. The secret is a capability for this one request; it is never
// shown and never leaves this origin.

import {ApiError} from './api.js';
import {hostIdOf} from './applink.js';

export const POLL_MS = 2000;

export async function requestPairing(client, {deviceId, label}) {
  const row = await client.pairingRequest({device_id: deviceId, device_name: label,
    client: {kind: 'web', label}});
  return {request_id: row.request_id, request_secret: row.request_secret, device_id: row.device_id,
    expires_at: row.expires_at, client_label: row.client_label || null, remote_addr: row.remote_addr || null,
    via: row.via || null};
}

// One look at the request: {state: 'pending'|'claimed'|'dead', credential?}
// The claim names the host (TAIL-3b L8 links into the app with its id).
export async function pollClaim(client, pending, now = Date.now()) {
  if (pending.expires_at && now / 1000 > pending.expires_at + 5) return {state: 'dead', reason: 'expired'};
  try {
    const result = await client.claim(pending.request_id, pending.request_secret);
    if (typeof result.credential === 'string' && result.credential) {
      return {state: 'claimed', credential: {token: result.credential, device_id: result.device_id,
        issued_at: result.issued_at, expires_at: result.credential_expires_at, host_name: result.host_name || null,
        host_id: hostIdOf(result.host_id), client_label: result.client_label || null}};
    }
    return {state: 'pending'};
  } catch (error) {
    if (error instanceof ApiError && [403, 404, 409].includes(error.status)) return {state: 'dead', reason: error.code};
    return {state: 'pending', error};
  }
}

// CORE-2 §1: in its last week a credential is traded for a new one; the old
// one keeps working for a day, so a reply lost on the way is harmless.
export async function renewIfDue(client, credential) {
  const info = await client.credential();
  if (!info || info.renewable !== true) return {...credential, expires_at: info && info.expires_at || credential.expires_at};
  const renewed = await client.renew();
  return {...credential, token: renewed.credential, issued_at: renewed.issued_at,
    expires_at: renewed.credential_expires_at};
}
