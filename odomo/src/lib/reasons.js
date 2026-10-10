// ODOMO-2: core's codes, in words (the App's `ReasonText`, herdr and agent
// domains). A code is never shown on its own: an unmapped one rides in
// brackets after a sentence, so it is still reportable.
import {fill} from './i18n.js';

export function herdrReason(t, code) {
  const known = t.reasons.herdr[code];
  return known || fill(t.reasons.herdrUnknown, code || '?');
}

export function agentReason(t, code) {
  const known = t.reasons.agent[code];
  return known || fill(t.reasons.agentUnknown, code || '?');
}

// An Agent panel error is a key in t.agent (this page's own sentence) or a
// code from the host.
export function agentErrorText(t, error) {
  if (!error) return '';
  if (error.key && t.agent[error.key]) return t.agent[error.key];
  return agentReason(t, error.code);
}

// What a failed request to the Herdr bridge says.
export function herdrErrorText(t, error) {
  return herdrReason(t, (error && error.code) || 'herdr_unavailable');
}
