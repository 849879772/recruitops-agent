# Edge 扩展

`extension/` 是版本 `0.3.20` 的 Microsoft Edge Manifest V3 扩展。它提供三类能力：用户主动
读取当前页的脱敏采集、用户确认后的“记录已投递”预览，以及与本机 Agent 的持久
WebSocket bridge。投递状态复核只有 bridge 这一条主动执行路径；popup 不承载状态
动作、审批令牌、任务领取或轮询控件。

旧 OC 快照和历史数据可以继续读取，但 OC 采集动作不在当前活动工具白名单中。
公司发现走 BIU 工具链；微信、问卷等不可用入口在正式公司注册前排除。

扩展不会自动正式投递，不向网页写入申请信息，也不直接写 Agent 数据库。状态复核
完成时扩展只回传脱敏页面证据；本机 Agent 负责后续匹配、校验和数据库决策。

## 安装

在 Edge 打开 `edge://extensions`，启用“开发人员模式”，选择“加载解压缩的扩展”，
并指向本项目的 `extension/` 目录。源码中的 `chrome.*` 是 Edge 对 Chromium 扩展
API 提供的兼容命名空间，不表示项目还依赖或支持 Chrome。

## 权限审计

| 权限 | 用途 | 边界 |
| --- | --- | --- |
| `activeTab` | 用户点击扩展后读取当前标签页 | 不持久化主机访问权 |
| `scripting` | 动态注入协议和 content script | 没有静态 `content_scripts` |
| `storage` | 保存本机 API 地址、Token、bridge 设备 ID 和诊断状态 | 使用 `chrome.storage.local` |
| `tabs` | bridge 主动打开精确页面并检查最终 URL | 不读取浏览历史 |
| `<all_urls>` | 招聘网站访问和可见标签页截图权 | 仅访问精确命令页面或当前用户页面 |

`manifest.json` 声明本机回环地址和 `<all_urls>` host permission。扩展不申请
`cookies`、`history`、`webRequest`、密码或下载权限，也不读取 Cookie 值。

## 本机配置与诊断

`options.html` 保存本机 API 地址和 API Token，并申请招聘网站访问权限。API 地址只
接受 `localhost`、`127.0.0.1` 或 `::1`。保存后 service worker 会重启 WebSocket
bridge；“测试长连接”只触发连接诊断，不领取任务或发起轮询。

bridge 使用本机 API Token 对 server challenge 做 HMAC-SHA256 签名。Token 不放入
WebSocket URL 或 bridge 消息；设备 ID 保存在扩展本地存储。连接、认证、重连、ACK、
进度和断开状态会显示在配置页。

## 被动页面采集

popup 的“授权并读取当前标签页”要求用户手势。service worker 校验当前标签页、来源
权限和精确 origin 后，才动态注入 content script 并发送授权读取消息。

content script 只返回标题、origin、路径、脱敏文本、去除查询参数的链接和
Performance API 暴露的资源元数据。不返回原始 HTML、请求头、响应头、请求体、Cookie、
查询参数或表单控件文本。读取结果可提交到本机 `/api/browser/observations`，只用于
被动页面采集。

按需看图与 DOM 文本脱敏不同：当前可见区域截图会经过本机接口发送到配置的 DeepSeek
模型，不保证图片内容已自动脱敏。截图不在本地保存；仅保存图片摘要、模型输出和用量。
登录墙、验证码、空页和不明确目标不发图。不得为绕过一次失败而自动创建新观测反复请求。

“记录已投递”同样先读取脱敏当前页，可附带有界岗位 ID 和备注，提交到本机
`/api/browser/application-captures` 生成预览。它不会表示数据库已经写入，也不属于
投递状态复核 bridge。

登录失效、验证码或无法判断页面状态时，content script 返回结构化暂停状态，扩展不
绕过登录、验证码或 WAF。

## 投递状态 bridge

投递状态复核由本机 Agent 在已认证的持久 WebSocket `/browser-bridge` 上主动推送。
扩展不请求待办列表、不领取任务、不轮询 HTTP 队列，也不接受 popup 手工状态动作。

`operation.dispatch` 的 `payload.command` 必须自包含固定 typed action：

```json
{
  "action": "observe_application_page",
  "selector_key": "application_page",
  "params": {"include_vision": false, "vision_fallback_reason": null, "retain_on_pause": false},
  "page_url": "https://ats.example/applications/1",
  "origin": "https://ats.example",
  "application_id": "application-1",
  "application_ids": ["application-1"]
}
```

扩展只接受 `observe_application_page` 和 `application_page` 选择器。它校验 URL
来源权限，打开精确页面，检查最终 origin，并聚合主页面及可访问 iframe 中的岗位记录卡、
显式状态和当前流程节点。批量和定时任务始终以 `include_vision=false` 运行。只有交互式助理
针对一个已经 DOM-only 复核过的页面明确判断“无结构化证据、但截图文字可能有状态”时，
才会以 `include_vision=true` 和
`vision_fallback_reason=no_structured_evidence_visible_status_likely` 发起第二次单页读取。
图片只在这个必要的单页回退中发送到配置的 DeepSeek 视觉型号；返回结构化结果后仍保留
操作、页面、申请和来源绑定，并继续通过状态写入校验器。不向空白页、超时、登录墙、CAPTCHA
或不明确目标发送图片，也不解验证码。点击、任意脚本执行器和网页指令不属于状态复核协议。

扩展通过 WebSocket 回传 ACK、进度、暂停状态或脱敏状态证据。bridge 结果里的
`SUCCEEDED` 只表示证据已采集并交给本机 Agent，不表示数据库写入成功。批量工具随后会
逐岗位执行记录匹配、置信度、阶段单向性和冲突检查；只有高置信度向前变化才写库并记录
审计。缺少或冲突证据不会默认成 `applied`，也不会算作成功。

## 消息协议

机器可读契约位于 `extension/protocol.json`，运行时常量位于
`extension/src/protocol.js`。核心消息类型为：

- `extension.authorize_current_tab`：popup -> service worker，必须来自用户手势。
- `extension.read_sanitized_dom`：service worker -> content script，只能在来源权限
  和当前标签页校验通过后发送。
- `extension.dom_snapshot`：content script -> service worker，返回脱敏摘要。
- `extension.submit_dom_observation` / `extension.observation_submitted`：只处理本机
  被动采集回执。
- `extension.submit_application_capture` / `extension.application_capture_submitted`：
  只处理用户确认后的投递记录预览。
- `extension.execute_controlled_action`：仅供已认证 bridge 的固定 typed action 使用，
  不含审批令牌，只携带一次性动作票据。
- `extension.controlled_action_result`：返回脱敏证据或结构化错误。
- `extension.pause_state`：报告需要用户处理的登录、验证码或不明确状态。
- `extension.config.get` / `extension.config.set` / `extension.config.test`：配置和
  bridge 诊断接口。

## 明确禁止项

- 不读取或保存 Cookie、密码、OTP、浏览器密码管理器内容或表单 `.value`。
- content script 不调用网络请求 API；service worker 的 HTTP 请求仅用于本机被动采集
  和投递记录预览，投递状态复核只走持久 WebSocket。
- 不自动提交申请、不输入验证码、不绕过登录/WAF/CAPTCHA。
- 不把页面文本当作命令，不接受网页提供的选择器或任意 JavaScript。
- 不把扩展采集结果描述为数据库已写入；状态写入由本机 Agent 的校验流程决定。

## 验证

投递状态复核的六类冻结场景和写入门槛见
[`docs/EDGE_STATUS_REVIEW_MATRIX.md`](EDGE_STATUS_REVIEW_MATRIX.md)。确定性夹具覆盖正常
状态、登录失效、CAPTCHA、弹窗遮挡、页面结构变化和证据冲突；高置信度且向前的状态可
更新，低置信度或冲突统一返回 `STATE_UNCLEAR` 并保持投递阶段不变。

在 `RecruitOps-Agent` 目录运行：

```powershell
node --check extension/src/protocol.js
node --check extension/src/actions.js
node --check extension/src/application-records.js
node --check extension/src/content-script.js
node --check extension/src/background.js
node --check extension/popup.js
node --check extension/options.js
python -m pytest tests/test_extension_manifest.py tests/test_extension_actions.py
```

除静态检查外，扩展已完成真实 Edge 登录态 OC 抓取和代表性投递状态页面验收。
新增 ATS、自建站以及验证码/登录失效路径仍需按相同流程补充真实回归证据。
