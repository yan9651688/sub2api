# LLMRelay 升级维护工具

本目录跟随定制分支维护源码、补丁、构建与部署校验。版本后缀固定为 llmrelay；构建必须显式传完整 --version，例如 0.2.3-llmrelay.1，不再推断旧的 availability 后缀。

build.py 使用 patches/series.json 中的精确上游提交与补丁，运行回归并生成带不可变镜像 ID 的发布清单。deploy.py 默认只读预览，--apply 才备份、切换应用和检查健康；失败时尝试恢复上一应用镜像。upgrade.sh/rollback.sh 是相同接口的 Shell 入口。

rehearse.py 在独立的内部 Docker 网络和临时数据卷运行升级、自动换号、监控与手动/自动回滚演练，结束时清理本次资源。不会连接生产数据或真实模型供应商。

仅接受相同迁移指纹或已审核的精确版本组合。0.2.3 迁移 236 修复分组模型白名单列；回退到 0.2.2 时保留兼容字段与 236 迁移登记，不恢复历史数据库备份。部署前必须完成数据库副本和应用回滚验证，详见 UPGRADE_0_2_3.md。

测试：在 Linux 上运行 python3 -m unittest discover -s tests -v。演练和部署需要 Linux、Docker Compose v2 与现有的项目备份脚本。
