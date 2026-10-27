# 插件市场发布前清单

依据2026-10-27查阅的 AstrBot 官方文档，区分官方要求与本项目建议，不将通过本地测试等同于审核通过。

## 官方发布流程

源码先放入 GitHub，metadata.yaml 提供身份、版本、作者及仓库等信息，再由作者注册 AstrBot Cloud 账号并在发布页面提交；ZIP不得超过16MB。[发布文档](https://docs.astrbot.app/dev/star/plugin-publish.html)

市场身份采用 author/name，元数据与市场记录需一致，repo 应是 HTTPS GitHub 仓库地址。[市场规范](https://docs.astrbot.app/dev/plugin-market/2026-06-27.html)

开发指南要求测试、错误处理、持久数据放data目录、Ruff格式化及异步网络请求；Logo为可选项，建议正方形256×256，平台及宿主版本范围也可声明。[开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)

## 当前已准备

- 稳定身份为 yuease/astrbot_plugin_bilibili_shixun，显示名B站拾讯，元数据repo已是HTTPS GitHub地址；已有GitHub仓库。v0.8.0的代码与发布包完成后再推送，不能把旧远端版本当作新版。
- 源码安装包白名单排除开发环境、运行数据、凭据和缓存；构建时检查16MB上限。依赖置于requirements.txt，持久数据在data/plugin_data下。
- 用户隔离、私聊权限、凭据不进入模型输出、联网与本地检索边界、错误码及资源上限已有实现和回归；README、PRIVACY.md、SOURCE.md和更新记录随包提供。
- 保守声明 aiocqhttp，AstrBot仍限定已测试的4.29.1，不把离线模拟外推到其他适配器或宿主版本。

## 发布前仍待处理

1. **确认分发许可。** 项目目前未声明LICENSE，SOURCE.md已说明自有上游来源和依赖许可；bilibili-api-python元数据为GPL-3.0-or-later。由作者明确许可证与分发安排，再补LICENSE及需要的说明。本轮不替作者决定许可，也不声称当前已完成许可审核。这是本项目发布准备事项，不把它说成上述官方文档列出的硬性条目。
2. **真实双用户联调。** 两个普通用户各自扫码、订阅、采集、查询、退出，交叉尝试条目ID和任务ID；确认通知只发本人。再用两个Dashboard账号验证默认分离及显式绑定。凭据不能进验收日志。
3. **干净环境验收。** 在Linux/Python 3.12/AstrBot 4.29.1中从仓库或ZIP安装依赖，验证初始化、重载、停用、后台任务恢复；测实际QQ图片投递和完整GUI。只有新增宿主版本验收通过后才扩大astrbot_version。
4. **资源与运维检查。** 多用户同时刷新、触发上限、用户数据清理、备份与恢复；确认SDK升级不会破坏任务上下文客户端隔离。现有小规模离线测试不代表高并发承载结论。
5. **发布材料与提交。** 可补Logo、操作截图、工具启用说明和问题反馈模板；更新版本源码，确认GitHub可被市场读取，并由作者登录AstrBot Cloud提交审核。不要用直接修改第三方市场列表替代当前官方流程。

建议先完成前3项，再提交市场。未制作Logo不等于不能提交；当前更重要的是许可决定与真实用户链路验收。本轮不代为提交市场或操作作者Cloud账号。
