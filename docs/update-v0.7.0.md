# v0.7.0：统一为 B站拾讯

插件包和目录为 `astrbot_plugin_bilibili_shixun`，默认档案标识为 `astrbot-bilibili-shixun`，页面路径为 `views/bilibili-shixun/`，桥接对象为 `BilibiliShixunTransport`，数据文件为 `workspace/bilibili_shixun.sqlite3`。页面 API 前缀随新插件标识同步更新。

公开工具按实际用途命名：

- `bilibili_search_videos_online`：临时联网搜索 B 站视频。
- `bilibili_search_saved_content`：检索已采集内容，不联网。
- `bilibili_read_content_detail`：读取条目正文与附件。
- `bilibili_get_subscriptions_and_preferences`：查看长期订阅和偏好。
- `bilibili_update_subscriptions_and_preferences`：修改长期关键词和兴趣。
- `bilibili_undo_settings_change`：撤销一次设置变更。
- `bilibili_get_source_status`：查看来源状态和采集覆盖。
- `bilibili_refresh_subscriptions`：刷新已有订阅或查看刷新进度。
- `bilibili_send_content_images`：按要求发送条目图片。

对模型暴露的工具说明与返回 guidance 均引用新工具名；不注册旧别名，避免出现重复入口。不改写原文标题和正文。内部操作键保持领域语义，不影响预算、幂等、鉴权和业务行为。

按用户要求，本版本采用全新安装，不提供旧数据迁移。先停用旧插件，再安装新包；重新启用人格中的工具、设置模型及订阅，需要关注来源时重新扫码。新数据目录为 `data/plugin_data/astrbot_plugin_bilibili_shixun/runtime-direct-v1/`。原目录不会被读取或删除，不应同时运行旧插件。

上游版权与来源链接保留真实名称；历史调查报告保留当时日志事实。原独立项目未改动。

验证：82项 Python 离线回归通过，包含真实 AstrBot 工具注册、名称唯一性及长度、说明交叉引用、原文不被重写和卸载清理；4项页面同步测试通过。Python 编译、Ruff 与两份 JavaScript 语法检查通过，安装包排除环境、运行数据、凭据及缓存。未部署或发送QQ消息。
