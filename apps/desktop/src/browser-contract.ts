export interface BackgroundPageLease {
  readonly id: number;
  readonly purpose: 'review';
  close(): void;
}

// No renderer IPC exposes script execution, parser injection or business writes.
export const browserIntegration = {
  connected: false,
  writesEnabled: false,
  reason: 'Evidence-only adapter; authenticated backend dispatch and confirmed capture persistence pending',
  fillSupported: false,
  fillCode: 'SHELL_FILL_NOT_CONNECTED'
} as const;
