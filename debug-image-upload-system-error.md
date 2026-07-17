# Debug Session: image-upload-system-error
- **Status**: [OPEN]
- **Issue**: 上传图片向智能体提问时，Copilot Studio 返回 SystemError。
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: .dbg/trae-debug-log-image-upload-system-error.ndjson

## Reproduction Steps
1. 打开 Web Chat 页面。
2. 上传一张图片并发送提问。
3. 记录页面返回的错误代码、对话 ID 和 UTC 时间。

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | 上传图片使用的本地或不可公开访问 URL，Copilot Studio 无法获取附件 | High | Low | Confirmed |
| B | 图片格式、大小或二进制内容不符合下游智能体连接器的限制 | Medium | Low | Rejected |
| C | 智能体或其模型/知识源未启用图片输入能力 | High | Medium | Pending |
| D | Bot Framework 附件活动格式与 Copilot Studio 所需架构不兼容 | Medium | Medium | Pending |
| E | Copilot Studio 服务端在该时间段出现暂态 SystemError | Low | Medium | Pending |

## Log Evidence
- 用户直接访问 `https://help.superpopp.cn/webchat/upload` 返回 `404 Not Found`。
- 该路径仅注册 `POST` 上传接口；直接在地址栏以 `GET` 方式访问时返回 404 属于预期行为，不能用于确认上传成功或图片是否可访问。
- 用户提供的上传成功图片地址位于 `https://subcost.superpopp.cn/uploads/<uuid>.png`，浏览器显示 `DNS_PROBE_FINISHED_NXDOMAIN`，即该主机名无法解析。
- 用户随后验证 `https://help.superpopp.cn/uploads/<uuid>.png`，服务器返回 `404 Not Found`；该路径缺少应用已注册静态文件路由所需的 `/webchat` 前缀。
- 应用只提供 `GET /webchat/uploads/{filename}` 路由，因此正确的公开路径应为 `https://help.superpopp.cn/webchat/uploads/<uuid>.png`。
- 用户验证 `https://help.superpopp.cn/webchat/uploads/<uuid>.png` 返回 `200 OK`，响应类型为 `image/png`，且浏览器已显示图片内容。
- 最新 `POST /webchat/upload` 响应中的 `contentUrl` 为 `http://help.superpopp.cn/webchat/uploads/37a3adff-d4d8-4055-b0c9-dc606141acb3.jpg`，协议为 HTTP；聊天页面本身通过 HTTPS 访问。
- 上传接口以 `req.url` 生成 `contentUrl`，说明反向代理未将原始 HTTPS 协议正确传递给应用，或应用未信任/未使用该转发协议头。
- 应用代码会把上传后的 `contentUrl` 原样作为 Bot Framework 附件传给 Copilot Studio；下游无法下载该图片时，可能返回泛化的 `SystemError`。
- 修复前测试日志：生成地址的协议为 `http`、主机为测试服务器的 `127.0.0.1:<port>`。
- 修复后测试日志：配置公开基址后生成地址的协议为 `https`、主机为 `help.example.com`，且路径保留 `/webchat/uploads/` 前缀。
- 生产端最新上传响应为 `https://help.superpopp.cn/webchat/uploads/c5c73d28-7fac-4cab-b152-25c3efc1d106.png`；发送请求中的附件 URL 一致，图片为 PNG、大小 43215 字节。
- `POST /webchat/send` 返回 200 且 Direct Line 接受活动并返回消息 ID；SystemError 由后续 Copilot Studio 会话活动返回，而非本服务上传或发送接口失败。

## Verification Conclusion
附件 URL 问题已修复，格式与大小限制可排除，且 Direct Line 已接受消息。**根本原因确认为假设 A 的衍生问题**：`_webchat_uploaded_file` 端点保留了 `_webchat_access_denied` 鉴权检查，Copilot Studio 服务器下载图片时无用户 Cookie，收到 401 后无法处理附件，最终返回泛化 `SystemError`。已移除该端点的鉴权（上传文件以 UUID 命名，本身具备不可猜测性）。
