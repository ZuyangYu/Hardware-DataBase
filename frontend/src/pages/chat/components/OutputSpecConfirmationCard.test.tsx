import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import OutputSpecConfirmationCard from './OutputSpecConfirmationCard';
import {
  parseDocumentCardEvent,
  planConfirmationInput,
  type DocumentCardData,
} from './documentCardModel';

function cardEvent(card: Record<string, unknown>): DocumentCardData {
  const parsed = parseDocumentCardEvent(JSON.stringify({ card }));
  if (!parsed) throw new Error('invalid card fixture');
  return parsed;
}

const proposalCard = cardEvent({
  kind: 'output_spec_confirmation',
  status: 'awaiting_plan_confirmation',
  next_actions: ['confirm_document_plan'],
  kb_name: 'hardware',
  task_id: 'task-1',
  generation_session_id: 'session-1',
  proposal: {
    document_plan_id: 'plan-1',
    document_plan_version: 1,
    plan_hash: 'sha256:plan',
    output_spec_id: 'spec-1',
    output_spec_version: 3,
    output_spec_hash: 'sha256:spec',
    status: 'awaiting_plan_confirmation',
    executable: true,
    deliverables: [{ format: 'xlsx', role: 'primary', required: true }],
    outline_count: 2,
    table_count: 1,
    warnings: ['来源中缺少部分管脚定义'],
    blockers: [],
  },
});

describe('OutputSpecConfirmationCard', () => {
  it('renders the what-will-be-generated summary without leaking hashes', () => {
    const markup = renderToStaticMarkup(
      <OutputSpecConfirmationCard card={proposalCard} confirming={false} onConfirm={() => undefined} />,
    );
    expect(markup).toContain('计划确认');
    expect(markup).toContain('xlsx');
    expect(markup).toContain('章节 2');
    expect(markup).toContain('表格 1');
    expect(markup).toContain('来源中缺少部分管脚定义');
    expect(markup).toContain('确认生成');
    expect(markup).toContain('修改需求');
    expect(markup).toContain('采用推荐方案');
    expect(markup).not.toContain('sha256:plan');
    expect(markup).not.toContain('sha256:spec');
  });

  it('disables confirmation while blockers exist or a request is in flight', () => {
    const blocked = cardEvent({
      kind: 'output_spec_confirmation',
      status: 'awaiting_plan_confirmation',
      next_actions: [],
      kb_name: 'hardware',
      proposal: { plan_hash: 'h', output_spec_hash: 's', blockers: ['模板不可用'] },
    });
    const blockedMarkup = renderToStaticMarkup(
      <OutputSpecConfirmationCard card={blocked} confirming={false} onConfirm={() => undefined} />,
    );
    expect(blockedMarkup).toContain('disabled');
    expect(blockedMarkup).toContain('模板不可用');

    const inFlight = renderToStaticMarkup(
      <OutputSpecConfirmationCard card={proposalCard} confirming onConfirm={() => undefined} />,
    );
    expect(inFlight).toContain('disabled');
  });

  it('shows the stale reconfirmation hint instead of auto-retrying', () => {
    const markup = renderToStaticMarkup(
      <OutputSpecConfirmationCard
        card={proposalCard}
        confirming={false}
        stale
        onConfirm={() => undefined}
      />,
    );
    expect(markup).toContain('计划已更新');
  });

  it('builds the exact-hash confirmation input from the visible proposal', () => {
    const input = planConfirmationInput(proposalCard, 'confirm-request-1');
    expect(input).toEqual({
      expected_output_spec_hash: 'sha256:spec',
      expected_plan_hash: 'sha256:plan',
      client_request_id: 'confirm-request-1',
    });
    expect(planConfirmationInput(proposalCard, '')).toBeNull();
    const noHash = cardEvent({
      kind: 'output_spec_confirmation',
      status: 'awaiting_plan_confirmation',
      next_actions: [],
      kb_name: 'hardware',
      proposal: { document_plan_id: 'plan-1' },
    });
    expect(planConfirmationInput(noHash, 'req')).toBeNull();
    expect(onConfirmGuard(proposalCard)).toBe(true);
  });
});

function onConfirmGuard(card: DocumentCardData): boolean {
  // The chat layer must only submit when the pure builder returns a complete
  // hash-bound payload; this mirrors the guard used by useKbChat.
  return planConfirmationInput(card, 'req') !== null;
}
