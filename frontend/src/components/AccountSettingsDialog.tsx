import { useState } from 'react';

import { api } from '../api/client';
import { ROLE_LABELS, type AuthSession } from '../auth';
import AppIcon from '@/components/AppIcon';
import PasswordInput from '@/components/PasswordInput';
import { Button, Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, Label } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  auth: AuthSession;
  onLogout: () => void;
};

/**
 * 个人设置弹窗:账号信息 + 修改密码 + 界面缩放 + 退出登录。
 *
 * 从头像菜单点开,不离开当前页面;修改密码需要当前密码,成功后服务端吊销
 * 该账号全部会话,这里直接走退出登录,让用户用新密码重新进入。
 */
export default function AccountSettingsDialog({ open, onOpenChange, auth, onLogout }: Props) {
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);

  async function submit() {
    if (!currentPassword || !newPassword) {
      notify.error('请填写当前密码和新密码');
      return;
    }
    if (newPassword.length < 8) {
      notify.error('密码长度不能少于 8 位');
      return;
    }
    if (newPassword === currentPassword) {
      notify.error('新密码不能与当前密码相同');
      return;
    }
    if (newPassword !== confirmPassword) {
      notify.error('两次输入的新密码不一致');
      return;
    }
    setSubmitting(true);
    try {
      await api.put('/api/v1/users/me/password', {
        current_password: currentPassword,
        new_password: newPassword,
      });
      notify.success('密码已修改，请用新密码重新登录');
      onLogout();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '修改密码失败');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] flex-col gap-0 rounded-[16px] p-0 ring-1 ring-[#e3e7f1] sm:max-w-[760px]">
        <DialogHeader className="gap-[4px] px-[24px] pt-[20px]">
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">个人设置</DialogTitle>
          <DialogDescription className="text-[12px] leading-[18px] text-[#757f9c]">
            账号信息与密码管理；修改密码后所有会话失效，需要用新密码重新登录。
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-[12px] overflow-y-auto px-[24px] py-[16px]">
          <section className="rounded-[12px] border border-[#e3e7f1] px-[16px] py-[14px]">
            <h3 className="text-[13px] font-semibold text-[#18181a]">账号信息</h3>
            <dl className="mt-[10px] grid gap-[8px] text-[13px]">
              <div className="grid grid-cols-[64px_1fr] items-center gap-[8px]">
                <dt className="text-[#757f9c]">用户名</dt>
                <dd className="break-all text-[#18181a]">{auth.user.username}</dd>
              </div>
              <div className="grid grid-cols-[64px_1fr] items-center gap-[8px]">
                <dt className="text-[#757f9c]">角色</dt>
                <dd className="break-all text-[#18181a]">{ROLE_LABELS[auth.user.role]}</dd>
              </div>
              {auth.user.department_name ? (
                <div className="grid grid-cols-[64px_1fr] items-center gap-[8px]">
                  <dt className="text-[#757f9c]">部门</dt>
                  <dd className="break-all text-[#18181a]">{auth.user.department_name}</dd>
                </div>
              ) : null}
            </dl>
          </section>

          <section className="rounded-[12px] border border-[#e3e7f1] px-[16px] py-[14px]">
            <h3 className="text-[13px] font-semibold text-[#18181a]">修改密码</h3>
            <p className="mt-[4px] text-[12px] text-[#757f9c]">至少 8 位；可点右侧眼睛图标核对输入。</p>
            <div className="mt-[12px] grid gap-[10px]">
              <div className="grid gap-[6px]">
                <Label className="text-[12px] text-[#464c5e]">当前密码</Label>
                <PasswordInput
                  autoComplete="current-password"
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  placeholder="输入当前密码"
                />
              </div>
              <div className="grid gap-[6px]">
                <Label className="text-[12px] text-[#464c5e]">新密码</Label>
                <PasswordInput
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  placeholder="至少 8 位"
                />
              </div>
              <div className="grid gap-[6px]">
                <Label className="text-[12px] text-[#464c5e]">确认新密码</Label>
                <PasswordInput
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder="再次输入新密码"
                />
              </div>
              <div className="flex justify-end">
                <Button
                  onClick={submit}
                  disabled={submitting}
                  className="h-[32px] min-w-[96px] rounded-[10px] bg-[#18181a] text-[14px] text-white hover:bg-[#303030]"
                >
                  {submitting ? '提交中' : '修改密码'}
                </Button>
              </div>
            </div>
          </section>

       </div>

        <div className="flex items-center justify-between gap-[8px] border-t border-[#eef1f6] px-[24px] py-[14px]">
          <span className="text-[12px] text-[#757f9c]">退出当前账号，需要重新登录。</span>
          <div className="flex items-center gap-[8px]">
            <Button
              variant="outline"
              onClick={() => onOpenChange(false)}
              className="h-[32px] rounded-[10px] border-[#e3e7f1] text-[14px] text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              关闭
            </Button>
            <Button
              variant="outline"
              onClick={onLogout}
              className="h-[32px] rounded-[10px] border-[#e3e7f1] text-[14px] text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              <AppIcon name="logout" size={16} />
              退出登录
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
