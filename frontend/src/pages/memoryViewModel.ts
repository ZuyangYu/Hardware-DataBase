import type { MemoryConsentView } from '../api/types';

export function visibleMemoryConsents(consents: MemoryConsentView[]): MemoryConsentView[] {
  return consents.filter((consent) => consent.status === 'active');
}
