# Git hooks

## pre-push：拦截对受保护分支（main / develop）的直接推送

启用（每个克隆执行一次）：

```bash
git config core.hooksPath hooks
```

生效后：

- 直接 `git push` 到 `main` / `develop` 会被拒绝（包括强推和删除）；
- 推送到功能分支不受影响；
- 确需绕过时使用 `git push --no-verify`（留痕，谨慎使用）。

规则依据：`.claude/skills/git-commit-skill/SKILL.md` 阶段 0。

> 注意：这是本地客户端拦截，只覆盖执行过上面配置的克隆；
> 若需服务端强制（任何客户端都拦得住），需要 GitHub Pro 的
> branch protection / ruleset。
