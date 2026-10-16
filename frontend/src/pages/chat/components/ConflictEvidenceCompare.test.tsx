import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import ConflictEvidenceCompare from './ConflictEvidenceCompare';

it('compares real source identities, conditions and approval without constructing primary-KB links', () => {
  const markup = renderToStaticMarkup(<ConflictEvidenceCompare claims={[{
    claim_id: 'b', source_kb_id: 205, source_role: 'reference', display_value: '12 V',
    condition: '输入', source_version_id: 'rev-2', source_content_hash: 'hash-2',
    evidence_ids: ['kb:205:record:9'], approval_status: 'draft', locator: { page: 3 },
  }]} />);
  for (const text of ['205', '参考', '12 V', '输入', 'rev-2', 'hash-2', 'kb:205:record:9', 'draft']) {
    expect(markup).toContain(text);
  }
  expect(markup).not.toContain('href=');
});
