import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui';

import AppIcon from './AppIcon';

type Props = {
  userName?: string;
  onLogout?: () => void;
};

export default function UserMenu({ userName, onLogout }: Props) {
  const initial = userName?.trim()?.[0]?.toUpperCase() ?? '--';

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label="账户菜单"
        className="flex h-[32px] shrink-0 items-center gap-[8px] rounded-[10px] pl-[4px] pr-[8px] outline-none"
      >
        <span className="grid size-[32px] shrink-0 place-items-center overflow-hidden rounded-full bg-[#eef1fb] text-[14px] font-medium leading-none text-[#7e96dc]">
          {initial}
        </span>
        <AppIcon name="arrow" size={14} className="shrink-0 text-[#757F9C]" style={{ transform: 'rotate(90deg)' }} />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-fit min-w-0 rounded-[14px] border-0 bg-white p-[6px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]"
      >
        <DropdownMenuItem
          onSelect={() => onLogout?.()}
          className="h-[36px] cursor-pointer gap-2 rounded-[10px] px-[12px] text-[14px] text-[#464C5E]"
        >
          <AppIcon name="logout" size={16} />
          退出登录
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
