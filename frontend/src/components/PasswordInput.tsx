import * as React from 'react';

import AppIcon from '@/components/AppIcon';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

type Props = Omit<React.ComponentProps<'input'>, 'type'> & {
  /** 外层容器 class(Input 自身的 class 仍走 className)。 */
  wrapperClassName?: string;
};

/**
 * 密码输入框:带「显示 / 隐藏」切换。
 *
 * 创建员工、重置密码这类需要把初始密码念给同事的场景,管理员可以点开
 * 明文核对;默认仍然是掩码,不主动显示。
 */
export default function PasswordInput({ className, wrapperClassName, ...props }: Props) {
  const [visible, setVisible] = React.useState(false);

  return (
    <div className={cn('relative', wrapperClassName)}>
      <Input
        type={visible ? 'text' : 'password'}
        className={cn('pr-[34px]', className)}
        {...props}
      />
      <button
        type="button"
        aria-label={visible ? '隐藏密码' : '显示密码'}
        title={visible ? '隐藏密码' : '显示密码'}
        onClick={() => setVisible((value) => !value)}
        className="absolute top-1/2 right-[10px] -translate-y-1/2 text-[#b3b8c4] transition-colors hover:text-[#18181a]"
      >
        <AppIcon name={visible ? 'eye-off' : 'eye'} size={16} />
      </button>
    </div>
  );
}
