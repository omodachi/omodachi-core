// What this browser calls itself on the approval card: "Safari on iPhone".
//
// It is a guess from the user agent and only ever a label - core cleans it
// like any requester-chosen name, prefixes it with "Web app", and authorizes
// nothing on it. iPadOS Safari reports itself as a Mac, so a Mac with a touch
// screen is an iPad.

export function browserName(ua) {
  const text = String(ua || '');
  if (/\bEdg(e|A|iOS)?\//.test(text)) return 'Edge';
  if (/\bFirefox\/|\bFxiOS\//.test(text)) return 'Firefox';
  if (/\bCriOS\/|\bChrome\/|HeadlessChrome\//.test(text)) return 'Chrome';
  if (/\bSafari\//.test(text) || /AppleWebKit/.test(text)) return 'Safari';
  return 'Browser';
}

export function platformName(ua, maxTouchPoints = 0) {
  const text = String(ua || '');
  if (/\biPad\b/.test(text)) return 'iPad';
  if (/\biPhone\b/.test(text)) return 'iPhone';
  if (/\bAndroid\b/.test(text)) return 'Android';
  if (/\bMacintosh\b/.test(text)) return maxTouchPoints > 1 ? 'iPad' : 'Mac';
  if (/\bWindows\b/.test(text)) return 'Windows';
  if (/\bCrOS\b/.test(text)) return 'ChromeOS';
  if (/\bLinux\b/.test(text)) return 'Linux';
  return 'this device';
}

export function browserLabel(ua, maxTouchPoints = 0) {
  return browserName(ua) + ' on ' + platformName(ua, maxTouchPoints);
}

// A device id the host has never seen, stable for this browser once stored.
export function newDeviceId(random = (n) => crypto.getRandomValues(new Uint8Array(n))) {
  return 'odomo-' + Array.from(random(8), (b) => b.toString(16).padStart(2, '0')).join('');
}
