import type { SkillInputRequest } from '../useKbChat';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

type Props = {
  request: SkillInputRequest;
  onRespond: (request: SkillInputRequest) => void;
  onDismiss: (taskId: string) => void;
};

/** Inline card for a Skill that stopped with needs_user_input. */
export default function SkillInputRequestCard({ request, onRespond, onDismiss }: Props) {
  return (
    <div data-skill-input-card={request.task_id} className="rounded-[12px] border border-[#d7e3f5] bg-[#f7faff] p-[14px]">
      <div className="flex flex-wrap items-center justify-between gap-[8px]">
        <div className="text-[14px] font-semibold text-[#18181a]">Skill 需要补充信息</div>
        <Badge variant="secondary">等待输入</Badge>
      </div>
      <div className="mt-[8px] break-all font-mono text-[12px] text-[#536077]">{request.skill_ref || 'Skill'}</div>
      {request.required_inputs.length > 0 ? (
        <ul className="mt-[8px] grid gap-[4px] text-[13px] leading-[1.6] text-[#464c5e]">
          {request.required_inputs.map((item) => <li key={item}>· {item}</li>)}
        </ul>
      ) : (
        <p className="mt-[8px] text-[13px] leading-[1.6] text-[#6b7280]">该 Skill 请求补充信息；请在下方说明缺项或提供所需参数。</p>
      )}
      <div className="mt-[12px] flex flex-wrap gap-[8px]">
        <Button onClick={() => onRespond(request)}>填写补充信息</Button>
        <Button variant="outline" onClick={() => onDismiss(request.task_id)}>稍后处理</Button>
      </div>
    </div>
  );
}
