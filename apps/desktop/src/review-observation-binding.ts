// Main-process-only provenance. Never serialized, page-provided, or sent to a model.
import type { WebContents } from 'electron';
type Identity = {wc: WebContents; url: string; frameToken?: string; epoch: number};
const bindings = new WeakMap<object, Identity>();
const epochs = new WeakMap<WebContents, {value: number}>();
export function captureReviewIdentity(wc: WebContents): Identity {
  let epoch = epochs.get(wc);
  if (!epoch) {
    epoch = {value: 0}; epochs.set(wc, epoch);
    const saved = epoch;
    wc.on?.('did-start-navigation', (_event, _url, _inPlace, isMainFrame) => { if (isMainFrame !== false) saved.value++; });
    wc.on?.('did-navigate-in-page', (_event, _url, isMainFrame) => { if (isMainFrame !== false) saved.value++; });
    wc.on?.('render-process-gone', () => { saved.value++; });
  }
  return {wc, url: wc.getURL(), frameToken: wc.mainFrame?.frameToken, epoch: epoch.value};
}
export function matchesReviewIdentity(wc: WebContents, binding: Identity): boolean {
  if (wc.isDestroyed() || wc.isLoadingMainFrame()) return false;
  const current = captureReviewIdentity(wc);
  return binding.wc === wc && binding.url === current.url && binding.frameToken === current.frameToken && binding.epoch === current.epoch;
}
export function bindReviewObservation<T>(value: T, binding: Identity): T {
  if (value && typeof value === 'object') bindings.set(value, binding);
  return value;
}
export function reviewObservationBinding(value: unknown) {
  return value && typeof value === 'object' ? bindings.get(value) : undefined;
}
export function inheritReviewObservation<T>(source: unknown, value: T): T {
  const binding = reviewObservationBinding(source);
  return binding ? bindReviewObservation(value, binding) : value;
}
