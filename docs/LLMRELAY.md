# LLMRelay 定制版

当前版本：`0.2.3-llmrelay.1`。维护分支：`codex/llmrepay`。

## 版本规则

2026-09-08：按用户更正，正式后缀为 `llmrelay`。此前误写的 `0.2.2-llmrepay.2`、`0.2.2-llmreplay.2` 标签作为历史记录保留；同一代码的名称修正版为 `0.2.2-llmrelay.2`。维护分支继续沿用原有路径，以保留已有链接。

使用 `<上游版本>-llmrelay.<修订号>`，固定保留 `llmrelay` 拼写。本次为 `0.2.3-llmrelay.1`；同一上游版本后续依次使用 `.2`、`.3`。升级上游基础版本时，将前面的版本同步为实际基础版本，并从修订号 `.1` 开始。

程序版本的源码入口为 `backend/cmd/server/VERSION`。发布时同步该文件、Git 标签、构建参数和镜像标签。Git 标签采用完整版本字符串，例如 `0.2.3-llmrelay.1`。历史的 `availability.1` 和临时的 `memory.1` 名称仅保留在历史发布记录中。

## 基础与功能

基于上游 `v0.2.3`，提交 `8fa67d477d6651a744754392a8982ea589c26ae6`。新增迁移 236 的验证要求与回退边界见 [迁移说明](../tools/llmrelay-maintenance/UPGRADE_0_2_3.md)。

保留原有 API Key 自动换号、账号与模型冷却、调度监控页面和上游分组测试夹具修正。此版本增加普通 Responses 请求的 bootstrap 检查内存优化。

自动化和任务转交两类 bootstrap 检查原先都会对整份正文检查重复成员并完整解码，即使请求不包含相关工具输出。现在先用 gjson 投影检查 `input` 中 `function_call_output` 的工具名。没有候选时直接返回原正文；存在候选时仍执行原有严格检查与转换。

## 内存优化验证

合入 0.2.3 后，11 个完整后端 unit 包（包含新增 migrations 包）通过，原内存优化和新 Ollama/账号测试修复一起参与回归。

- 10 个完整后端 unit 测试包通过，包含 handler、service、repository 与 config。
- 403,503 次差分 fuzz 执行通过，对比优化入口与原始严格实现。
- Linux/amd64 隔离容器中的完整 handler 测试通过。
- 合成普通历史请求在两次 bootstrap 检查中的累计分配如下（Go 1.27.1，Darwin/arm64，三次测量中位数）：

| 正文大小 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 1 MiB | 20.02 MiB | 32 B |
| 8 MiB | 160.02 MiB | 32 B |
| 32 MiB | 640.02 MiB | 32 B |

这些结果仅代表该检查步骤的累计分配，不是整次请求或进程 RSS。输入正文在计时前构建。回归测试、差分 fuzz 和可复现基准位于 `backend/internal/handler/openai_bootstrap_memory_test.go`。长时间运行的内存表现仍需结合实际负载观察。

## 构建与升级

`backend/scripts/resolve-version.sh` 会优先读取当前提交的精确版本标签，再读取 VERSION 文件。构建时可显式指定完整版本，例如在仓库根目录：

```sh
RELEASE_VERSION=$(cat backend/cmd/server/VERSION)
RELEASE_COMMIT=$(git rev-parse HEAD)
docker build --build-arg VERSION="$RELEASE_VERSION" \
  --build-arg COMMIT="$RELEASE_COMMIT" \
  -t "local/sub2api:$RELEASE_VERSION" .
```

部署前核对二进制版本、不可变镜像 ID、迁移指纹，并保留旧镜像、配置和数据库备份。使用现有维护工具时必须显式传入完整的 `--version`，避免旧工具的 `availability.1` 默认后缀。

网页中的官方升级入口仍指向官方程序，可能覆盖此分支的定制补丁。后续定制版升级应从本分支构建并按已有维护流程部署。
