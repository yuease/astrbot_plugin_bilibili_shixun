# 2026-10-27：附件、等待、刷新说明与原图

本轮依据用户要求补齐三项复测发现，并按追加要求支持原图发送。

## 附件与图片

列表直接提供 image_count、images_status、attachment_types、has_attachments、attachments_status。有图可用 has_images=true 查询；false 仅匹配明确无已采集内容图片的条目，未知记录不会混入。动态类别与图片数量分开，投票不算图片，视频封面和投票选项图片不纳入内容图片数量。

详情 attachments 包含已解析的图片、投票、视频、专栏、音频、直播、预约、链接及未知类型。投票可展示 question 和 options[].text；动态页没有选项时，采集中通过已固定版本 SDK 的只读投票接口补读。每批最多两次，每次三秒且受来源总预算限制，按账号缓存一分钟。预算不足保留 partial，补读失败保留 unavailable，不伪造空选项为完整信息，也不使整批动态失败。问题、选项有文本和数量上限，达到上限仍标部分。未识别附件保留 unknown，不默认为没有。

现有记录保持原样，缺少元数据时默认未知；刷新来源后更新相应条目。付费表情占位符转换为可读表情名。简报也接收附件信息。

## 等待与结果

refresh_sources 的 start/status 均支持 wait_seconds，默认十秒，允许零至二十秒。等待的是确切任务，等待超时不会取消后台采集；重复和并发刷新复用现有任务。运行中可再次 status 有限等待，工具不要求模型执行 shell 或 sleep。

operation_finished 表示任务到达 completed/failed；operation_succeeded 表示任务流程保存了可用结果，不能替代数据覆盖判断。data_completeness 为 incomplete/unknown；对应 data_complete 为 false/null，当前不宣称全部更新已取完。warnings 始终为数组，页数/时间/请求上限、分页异常、单页采样和来源失败分别说明。source_item_count 为跨来源未去重计数，retained_candidate_count 为候选数，sources 给出各来源本轮停止原因。旧任务缺少这些汇总时保持未知。所有结构化时间仍按北京时间输出。

## 原图

send_item_images 默认 quality=original，去掉已知格式的 CDN @缩图后缀，在验证格式和像素限制后直接发送下载字节。preview 才使用 1080×1620、2 MiB JPEG 预览；动图原图保留原始文件，预览只取第一帧。

两种模式均限制单图下载 12 MiB、3200 万像素、每次三张，并拒绝任意地址和重定向。原图超限或投递失败明确返回错误，不自动改用预览。sent_details 说明实际模式、字节数和原始字节是否保留。原图保留指插件发送端不重编码，不对 QQ 后续处理作保证。

全量 51 项离线检查通过，包含真实 SDK/宿主类型和模拟网络、消息发送。未使用真实账号或线上投票，不代表部署环境的 B 站风控和 QQ 投递已验收。保留 runtime-direct-v1、现有登录和采集数据，不导入更早的 runtime/runtime-v2。
