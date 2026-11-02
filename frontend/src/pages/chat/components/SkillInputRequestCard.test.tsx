import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import SkillInputRequestCard from './SkillInputRequestCard';

describe('SkillInputRequestCard', () => {
  it('shows required inputs and offers an explicit response action', () => {
    const markup = renderToStaticMarkup(
      <SkillInputRequestCard
        request={{
          task_id: 'task-1',
          skill_ref: 'skill/report',
          required_inputs: ['电源电压', '目标器件'],
          checkpoint_artifact_id: 'art-1',
        }}
        onRespond={() => undefined}
        onDismiss={() => undefined}
      />,
    );
    expect(markup).toContain('data-skill-input-card="task-1"');
    expect(markup).toContain('Skill 需要补充信息');
    expect(markup).toContain('电源电压');
    expect(markup).toContain('填写补充信息');
    expect(markup).toContain('稍后处理');
  });
});
