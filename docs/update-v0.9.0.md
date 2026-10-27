# v0.9.0：QQ 官方机器人适配，暂未测试

按用户要求，本轮只完成实现、静态检查和打包，不运行自动测试、真实扫码、模型调用、QQ消息发送或服务器部署。不增加WebUI登录入口。v0.8.0的99项Python和5项页面测试结果仅为上一版基线，不能作为本轮适配已通过的证据。

## 支持范围

metadata.yaml增加qq_official与qq_official_webhook。两者仅适配QQ单聊C2C；QQ群、频道及频道私信不开放。本插件通过AstrBot 4.29.1的C2C事件识别单聊，保持现有用户隔离，不将群或频道事件当成个人账号操作。

QQ官方采用OpenID标识用户，不是QQ号码；私聊档案仍由适配器、机器人实例ID及框架提供的用户ID共同决定。同一人使用OneBot和QQ官方机器人会有不同档案，不做猜测合并。Dashboard既有绑定配置可继续使用，但填写的用户ID必须是实际user_identity中的OpenID。此处不新增网页登录能力。

## 消息与账号

- /B站登录、/B站状态、/B站退出沿用原入口。二维码采用Image.fromBytes，经AstrBot官方适配器上传；状态及命令回复明确关闭原生Markdown，避免要求额外Markdown权限。
- 图片仍每条消息链只含一张图，使用当前用户事件被动回复，不直接调用腾讯HTTP接口或OneBot方法。说明不附加URL并清理标题里的URL，原文链接仍保留在工具结果供查询。Bot最终回复若含链接，可能需要在开放平台预先配置消息URL域名。
- 官方模式不依赖后台主动推送，确认登录失效后，在用户再次调用插件时返回account_notice；用户手动发送登录命令获取新二维码。OneBot原有自动重扫提醒保留。
- 二维码发送增加等待上限；已成功保存凭据后，结果通知失败只记录login_notice_failed，不再误报整个扫码登录失败、不自动重扫。用户可通过状态命令查看实际状态。
- 图片发送仍遵循下载/像素/条数上限，发送结果不确定时不自动重发。送入AstrBot的原图字节保留，不保证QQ客户端最终展示不会被平台压缩。

## 成本和限制

不增加模型调用、常驻进程或数据库。搜索、订阅、候选池和用户隔离均复用；新增开销主要是二维码及图片上传，流量随使用量变化。不承诺腾讯接口无费用或无配额限制，具体以开发者后台为准。平台审核、媒体上传、回复频率及消息URL权限不能由插件绕过。

本轮保留固定AstrBot版本范围，不将本地阅读源码等同于线上功能验证。待用户允许测试后，需验证C2C真实事件、二维码发送和成功/超时通知、原图、OpenID隔离、Webhook和WebSocket、链接域名限制，以及原有OneBot回归。

## 实现依据

- [AstrBot QQ官方平台文档](https://docs.astrbot.app/platform/qqofficial.html)：官方接入区分Webhook和WebSocket。
- [AstrBot发送消息文档](https://docs.astrbot.app/dev/star/guides/send-message.html)：消息链和事件回复由宿主处理。
- 已安装AstrBot 4.29.1源码中的QQOfficialMessageEvent、QQOfficialPlatformAdapter及Webhook子类：确认C2C图片上传与消息链支持，未修改这些宿主源码。
- [腾讯消息文档](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/send-receive/send.md)标注主动推送停止提供及消息URL配置要求，因此采用不依赖主动推送的默认方案。SDK存在发送接口不等于账号具有平台发送权限。
