# 代码来源与维护

直连版使用 PyPI 的 [bilibili-api-python 17.4.2](https://pypi.org/project/bilibili-api-python/17.4.2/) 作为依赖，遵循其包内 GPL-3.0-or-later 许可；不复制或修改其源码。SDK 接口按该版本公开发行源码核对。

QQ 扫码登录流程参考 [c1overzz/dsh-bili-scroll 的 login.mjs](https://github.com/c1overzz/dsh-bili-scroll/blob/d75464ba6ac9f4e704364f0d6feb2674b726c818/lib/login.mjs)，本插件独立实现 Python 登录、轮询、凭据存储和私聊通知，没有引入其执行环境或评论、私信等功能。

v0.3 已移除 RSS 解析与 RSSHub 请求逻辑，直接维护 B 站 JSON 到插件内容模型的转换。

本插件的采集、规则、候选池、排名、反馈和存储实现，源于同一作者的 [yuease/info-agent](https://github.com/yuease/info-agent)，基线提交为 `a4b7a854eb01a176093a1cfc5e4ba69aaf623aa4`。

v0.1 曾将此基线作为固定 vendor 快照，在独立进程中运行。v0.2 将可复用的领域代码移入 `core/` 并由插件直接维护：内部导入改成相对路径；移除了 LangGraph、独立模型客户端、模型证据筛选与查询缓存；页面改为直接维护；保留来源、过滤、存储、偏好与简报排序逻辑。

现在的 `core/` 已经修改，不再声称与上游逐字节一致，不再使用固定快照的校验、同步或页面生成脚本。更新上游应逐项审阅变更后移植。

所核对的上游基线没有 LICENSE、COPYING 或 NOTICE 文件；本插件没有凭空添加上游许可。插件仓库为 [yuease/astrbot_plugin_bilibili_shixun](https://github.com/yuease/astrbot_plugin_bilibili_shixun)，当前未声明插件开源许可证。

重构前源码已保存在本次工作的本地工作目录中。旧版开发环境 `.dev/` 与历史部署记录未删除，也不进入新版安装包。
