---
name: commit-message
description: 根据当前未提交的改动生成规范的 git 提交信息。当用户说「写提交信息」「帮我 commit」「生成 commit message」时使用。
---

# 生成提交信息

1. 调用 `git_changes`（用户已 `git add` 时传 `staged=true`）拿到改动。
2. 按 Conventional Commits 格式输出：

   ```
   <type>(<scope>): <一句话摘要，不超过 50 字>

   - 改了什么、为什么改（每条一行）
   - 有破坏性变更时加一行 BREAKING CHANGE: ...
   ```

   type 取值：feat / fix / refactor / perf / test / docs / build / ci / chore。
3. 只输出提交信息，不要自己执行 `git commit`，除非用户明确要求。
