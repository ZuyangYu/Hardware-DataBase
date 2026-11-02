const DISABLED_SKILLS_KEY = 'hdb.skill.disabled_refs';
const MAX_DISABLED_SKILLS = 32;

function storage(): Storage | null {
  if (typeof window !== 'undefined' && window.localStorage) return window.localStorage;
  const globalStorage = (globalThis as typeof globalThis & { localStorage?: Storage }).localStorage;
  return globalStorage ?? null;
}

export function readDisabledSkillRefs(): string[] {
  try {
    const raw = storage()?.getItem(DISABLED_SKILLS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
        .map((item) => item.trim())
        .filter((item, index, values) => values.indexOf(item) === index)
        .slice(0, MAX_DISABLED_SKILLS)
      : [];
  } catch {
    return [];
  }
}

export function writeDisabledSkillRefs(refs: string[]): string[] {
  const normalized = refs
    .map((ref) => ref.trim())
    .filter((ref, index, values) => ref && values.indexOf(ref) === index)
    .slice(0, MAX_DISABLED_SKILLS);
  try {
    if (normalized.length > 0) storage()?.setItem(DISABLED_SKILLS_KEY, JSON.stringify(normalized));
    else storage()?.removeItem(DISABLED_SKILLS_KEY);
  } catch {
    // A storage restriction should not block chat or Skill subscription.
  }
  return normalized;
}

export function toggleDisabledSkillRef(ref: string): string[] {
  const current = new Set(readDisabledSkillRefs());
  if (current.has(ref)) current.delete(ref);
  else current.add(ref);
  return writeDisabledSkillRefs([...current]);
}

/** Clear every temporary suppression; subscriptions and permissions stay intact. */
export function clearDisabledSkillRefs(): string[] {
  return writeDisabledSkillRefs([]);
}
