# Microsoft 365 Agents SDK WebChat 与 FAQ 管理平台

## 项目概述

本项目是一个基于 Python、Aiohttp 与 Microsoft 365 Agents SDK 构建的单进程 Web 应用。它将 Agent 兼容消息接口、自定义 WebChat、Copilot Studio 接入、FAQ 知识库和管理员后台整合在一个服务中。

项目面向需要快速交付内部智能问答入口的业务团队、开发人员、运维人员和内容运营人员，解决以下问题：

- 为 Microsoft 365 Agent 或 Copilot Studio 提供可嵌入、可运营的 WebChat 访问入口。
- 在未接入上游 Agent 时，以本地回显模式完成页面、接口与集成流程联调。
- 由非开发人员在后台维护 FAQ、导入知识内容、查看用户提问记录。
- 通过管理员登录、MFA、CSRF 防护及可选 Entra ID 登录审批，建立基础访问控制边界。

默认使用本地回显模式，适用于开发和演示；也支持通过 Direct Line 或 Copilot Studio Conversations API 对接实际智能体服务。

## 角色与适用场景

| 角色 | 可使用能力 |
| --- | --- |
| 终端用户 | 未启用 Entra ID 时可匿名使用 WebChat；启用后，完成登录且获管理员批准的用户可使用 WebChat、查看已发布 FAQ 详情及上传受限类型附件。FAQ 目录当前仅返回已发布条目的元数据，不校验登录或审批状态。 |
| 内容运营人员 | 登录管理后台，创建、编辑、发布、排序 FAQ，导入 Markdown 或 DOCX 内容。 |
| 平台管理员 | 管理管理员账号和 MFA、审核 Entra 登录用户、查看问题日志与连接配置状态。 |
| 开发人员 | 本地回显调试、运行回归测试、接入 Direct Line 或 Copilot Studio。 |
| 运维人员 | 配置机密环境变量、持久化 SQLite 数据、通过反向代理发布 HTTPS 服务并执行健康检查。 |

## 功能清单

### 核心功能

| 模块 | 功能 | 状态 | 说明 |
| --- | --- | --- | --- |
| Agent 接口 | `POST /api/messages` | 可用 | 由 Microsoft 365 Agents SDK 的 Aiohttp Hosting 处理活动。 |
| Agent 接口 | `GET /api/messages` 健康检查 | 可用 | 返回 HTTP 200，不依赖上游 Agent。 |
| 本地聊天 | 会话创建、发送、轮询 | 可用 | 默认 `local` 模式，服务端返回欢迎语和回显消息。 |
| Copilot Studio | Direct Line 模式 | 可用，需配置 | 服务端获取 Direct Line token，创建、发送并轮询远端会话。 |
| Copilot Studio | Conversations 代理模式 | 可用，需配置 | 支持原始 Bearer Token 或 AAD 客户端凭据。 |
| WebChat | 多会话、历史恢复、消息去重 | 可用 | 浏览器使用 `localStorage` 保存会话、watermark 和消息历史。 |
| WebChat | 附件上传与下载 | 可用 | 发送最多 5 个附件；限制 PNG/JPG/WEBP/TXT/LOG 类型与大小。 |
| FAQ | 已发布目录 | 可用 | `GET /faq/directory` 返回已发布 FAQ 的 ID、标题和分类；当前不执行 Entra ID 登录或审批校验。 |
| FAQ | 已发布详情与访问日志 | 可用，依赖 Entra ID | 启用 Entra ID 后，仅 `approved` 用户可查看已发布 FAQ 正文，访问会记入问题日志；未启用 Entra ID 时详情页返回 HTTP 503。 |
| FAQ | 页面大纲 | 可用 | 正文包含 Markdown 一级至三级标题时，桌面端生成粘性“本页目录”并随阅读位置高亮；窄屏（≤760px）隐藏大纲。 |
| FAQ | 创建、编辑、删除、发布、排序 | 可用 | 由管理员后台维护。 |
| FAQ | Markdown/DOCX 导入 | 可用 | 支持正文、基础样式、表格和 DOCX 嵌入图片转换。 |
| 管理后台 | FAQ、日志、访问授权、账号管理 | 可用 | 单页后台按面板加载数据。 |
| 管理员安全 | 密码登录、验证码、TOTP MFA、CSRF | 可用 | MFA 为账号级配置，后台写操作需要 CSRF 令牌。 |
| Entra ID | 全球版与世纪互联登录 | 可用，需配置 | 客户端 ID 与密钥均配置后启用对应登录入口。 |
| Entra ID | 登录后审批 | 可用，需配置 | 首次登录为 `pending`，仅 `approved` 用户可使用 WebChat API。 |
| 界面主题 | WebChat 明暗主题 | 可用 | WebChat 初始外观跟随系统主题，账户区图标支持临时切换；不持久化用户选择。 |

### 辅助功能与边界

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| `/help` 与成员加入欢迎语 | 可用 | 默认 Agent 返回快速入门帮助文本。 |
| 配置状态检查 | 可用 | `/admin/connection-status` 仅校验配置完整性，不探测上游连通性。 |
| HTTP 顺序基准 | 可用 | `benchmarks/http_baseline.py` 可对指定 URL 发送顺序请求。 |
| Teams App Tester | 可选 | 需自行安装 `@microsoft/teams-app-test-tool`。 |
| 横向扩容 | 不支持直接使用 | 会话、Token 缓存和授权会话保存在进程内存。 |
| 离线前端资源 | 未内置 | 聊天页、后台及登录页包含 CDN 依赖；受限网络生产环境应自行内化静态资源。 |

## 技术架构

### 技术栈与选型

| 层级 | 技术 | 选型作用 |
| --- | --- | --- |
| 运行时 | Python | 提供轻量、可直接运行的服务端实现。 |
| Web 框架 | Aiohttp | 承载异步 HTTP 路由、文件上传、子应用和长轮询。 |
| Agent 运行时 | Microsoft 365 Agents SDK | 暴露 Agent 活动端点并处理 Agent 消息。 |
| 视图层 | Jinja2、原生 JavaScript/CSS | 服务端渲染页面与浏览器端聊天交互。 |
| 数据持久化 | SQLite | 存储 FAQ、管理员、问题日志和 WebChat 授权记录。 |
| 身份认证 | MSAL、OAuth 2.0、Microsoft Graph | 完成全球版与世纪互联 Entra ID 授权码登录和头像读取。 |
| 上游集成 | Direct Line、Copilot Studio Conversations API | 连接 Copilot Studio 智能体。 |

### 分层与代码组织

```text
app.py
  └─ 创建 AgentApplication，注册欢迎语、/help 与回显消息，启动服务

start_server.py
  ├─ Aiohttp 应用工厂与路由装配
  ├─ WebChat 会话、附件、上游 Copilot Studio 编排
  ├─ Entra ID OAuth 登录、会话及访问审批
  └─ Agent API 子应用挂载

faq.py
  ├─ SQLite 初始化、迁移与数据访问
  ├─ FAQ 与 Markdown/DOCX 内容处理
  ├─ 管理员认证、MFA、CSRF、问题日志
  └─ 管理后台页面与 API 处理

templates/
  ├─ webchat.html：聊天、FAQ 浏览和浏览器本地会话管理
  ├─ webchat_login.html：Entra 登录入口
  ├─ webchat_pending.html：待审批/拒绝页面
  ├─ admin.html：admin 单页后台
  └─ admin_login.html：管理员登录页
```

### 系统交互流程

1. `app.py` 使用 `python-dotenv` 读取 `.env`，创建使用内存存储的 `AgentApplication`。
2. `start_server.py` 调用 `initialize_database()`，创建 Aiohttp 主应用及挂载于 `/api` 的 Agent 子应用。
3. 用户访问 `/webchat`：未启用 Entra 时可直接访问；启用后需完成 OAuth 登录并通过管理员审批。
4. 浏览器调用 `/webchat/start` 创建会话，随后通过 `/webchat/send` 发送消息、`/webchat/poll` 轮询新活动。
5. 服务端按 `WEBCHAT_BACKEND` 选择本地回显、Direct Line 或 Conversations API；前端按 watermark 去重并将历史保存至浏览器本地。
6. FAQ、管理员、日志和访问授权写入 `data/faq.db`；上传附件及 DOCX 导入图片存入 `data/uploads/`。

### 界面与本地偏好

- WebChat 桌面端采用三列布局：左侧包含新建对话、浏览器本地保存的历史会话和账户菜单，中间为聊天区，右侧为已发布 FAQ 标题列表。点击 FAQ 会在新标签页打开详情。
- 视口宽度不大于 720px 时，WebChat 左右侧栏均会隐藏；当前未提供移动端抽屉或 FAQ 的替代入口。
- WebChat 初始明暗外观跟随操作系统，账户区域的图标可临时切换主题；该选择不写入浏览器持久化存储，系统主题变更会再次同步页面外观。
- WebChat 登录页与待审批页仅自动跟随系统主题，不提供手动切换。FAQ 详情页和管理后台使用独立的浅色界面；管理后台在视口宽度不大于 900px 时由左侧面板改为顶部横向换行布局。

### 运行与扩展约束

- 服务固定监听 `0.0.0.0:5300`，当前代码没有 Host/Port 环境变量覆盖项。
- SQLite、管理员会话、Entra 会话、本地聊天会话、Direct Line Token 缓存均为单进程设计。多进程或横向扩容前，必须将会话和共享状态迁移到外部存储。
- 应用未提供 Dockerfile、IaC、CI/CD、反向代理或 HTTPS 终止配置；生产环境需要由部署平台补齐。

## 核心模块实现细节

### Agent 消息模块

- 文件：[app.py](app.py)。
- `AgentApplication` 使用 `MemoryStorage` 和 `CloudAdapter` 创建；成员加入和 `/help` 都返回欢迎帮助文本。
- 默认的 `message` 活动处理器返回 `you said: <文本>`，因此本地 Agent 接口本质是用于联调的回显示例，而不是业务推理引擎。
- `/api/messages` 接收活动，`GET /api/messages` 为不依赖外部服务的健康检查。

### WebChat 与上游连接模块

- 文件：[start_server.py](start_server.py)。
- `local` 是默认模式，使用进程内字典保存会话活动，首条活动为欢迎语；适用于开发、演示和端到端页面验证。
- `directline` 模式向配置的 token endpoint 获取 Direct Line Token，服务端负责会话创建、消息转发、活动轮询和 Token 过期管理。
- `copilotstudio` 模式代理 Conversations API，支持 `raw` JWT 与 `aad_client_credentials` 两种 Token 获取方式；匿名入口不得使用含 `/authenticated/` 的会话地址。
- 前端使用 `localStorage` 存储会话 ID、watermark、消息和已处理活动 ID；刷新后尝试恢复，失败时创建新会话。
- 上传功能默认关闭。设置 `WEBCHAT_UPLOADS_ENABLED=true` 启用附件接口；设置 `WEBCHAT_UPLOAD_BUTTON_VISIBLE=true` 显示输入框的“+”上传按钮。两个参数可独立配置。上传单文件最大 5 MB，单次消息最多 5 个附件；图片按文件签名验证，文本要求 UTF-8 且不含 NUL 字节。生产环境应设置 `WEBCHAT_PUBLIC_BASE_URL` 为可从公网访问的 HTTPS 根地址，确保 Copilot Studio 能获取上传附件。`data/uploads/` 会在服务启动时按 `WEBCHAT_UPLOAD_RETENTION_DAYS` 清理过期文件，默认保留 10 天。FAQ 编辑器插入及 DOCX 导入的图片独立保存至 `data/images/`。

### FAQ 与内容导入模块

- 文件：[faq.py](faq.py)。
- FAQ 实体包含标题、分类、摘要、正文、排序值、发布状态和时间戳；正文继续以受控 Markdown 持久化，后台默认以所见即所得方式编辑，并在提交时转换回 Markdown；正文资源仅允许引用 `/faq/images/` 中的受控图片；前台查询使用 `(is_published, sort_order, id)` 索引。
- Markdown 在服务端转义后仅渲染受控的标题、列表、代码、引用、链接、图片和基础行内样式；链接仅允许 `http`、`https` 与 `/webchat/uploads/` 路径。
- 导入支持 `.md`、`.markdown` 与 `.docx`；DOCX 解析文字样式、列表、表格和嵌入图片，图片保存后转为内部 Markdown 引用。
- 导入源文件最大 10 MB，正文最大 50,000 字符；前台接口不会返回未发布 FAQ。
- FAQ 详情仅支持已批准用户访问：未登录用户会进入 Entra ID 登录页面，待审批、拒绝或已撤销用户会进入对应状态页。正文含一级至三级标题时，详情页会生成唯一锚点和左侧阅读大纲；访问详情会记录问题日志。

### 管理后台与安全模块

- 文件：[faq.py](faq.py) 和 [templates/admin.html](templates/admin.html)。
- 首次初始化时，只有在 `admin_accounts` 表为空且设置 `FAQ_ADMIN_PASSWORD` 时才创建初始管理员。
- 密码使用随机盐的 PBKDF2-HMAC-SHA256（120,000 次迭代）保存，认证比较采用恒定时间比较。
- 管理员会话 Cookie 为随机 Token 与 HMAC 签名组合，有效期 8 小时；后台写操作校验表单 CSRF Token 或 `X-CSRF-Token`。
- 管理员账号可独立启用 RFC 6238 风格的 TOTP MFA；管理员登录根据账号 MFA 状态校验 OTP 或验证码。
- 问题日志记录提问时间、来源 IP、登录账号、User-Agent 推导的系统/浏览器信息和消息文本；按 `FAQ_LOG_RETENTION_DAYS` 清理，查询最多返回 500 条。

### Entra ID 身份与访问审批模块

- 文件：[start_server.py](start_server.py)。
- 同时提供全球版和世纪互联 Entra ID 授权码登录入口，使用 MSAL 获取用户身份信息与 Microsoft Graph 头像。
- 同时配置客户端 ID 与客户端密钥后才启用对应登录；未配置 Entra 时，WebChat 不要求登录。
- 启用后，系统以 `(tenant_id, object_id)` 唯一标识用户。首次登录创建 `pending` 授权记录，管理员可将其变更为 `approved`、`denied` 或 `revoked`。
- 只有 `approved` 状态可调用受保护的 WebChat 接口并访问 FAQ 详情；待审批、拒绝或已撤销用户会进入专用状态页或收到拒绝响应。`/faq/directory` 当前不受该审批校验保护。

### 数据持久化模块

- 数据库固定位置为 `data/faq.db`，使用 SQLite WAL、外键约束和 5 秒 busy timeout。
- 包含 `faqs`、`admin_accounts`、`webchat_question_logs`、`webchat_access_grants` 四张核心表，并在启动时执行幂等建表、索引创建与兼容性迁移。
- `data/faq.db` 与 `data/uploads/` 是生产业务数据，必须作为持久化卷或受控备份对象，不能视为可清理的构建产物。

## 接口概览

| 分类 | 路径 | 方法 | 用途 |
| --- | --- | --- | --- |
| Agent | `/api/messages` | POST | 接收 Agent 活动。 |
| Agent | `/api/messages` | GET | 健康检查。 |
| WebChat | `/`、`/webchat` | GET | 聊天首页。 |
| WebChat | `/webchat/config` | GET | 返回当前模式与配置状态。 |
| WebChat | `/webchat/start`、`/webchat/send`、`/webchat/poll` | POST | 创建会话、发送消息、轮询活动。 |
| WebChat | `/webchat/upload` | POST | 上传受限类型附件。 |
| WebChat | `/webchat/uploads/{filename}` | GET | 读取权限受控的附件。 |
| FAQ | `/faq/directory` | GET | 查询已发布 FAQ 的 ID、标题和分类，不要求登录。 |
| FAQ | `/faq/{faq_id}` | GET | 查询单个已发布 FAQ；需要 Entra ID 已批准用户。 |
| Entra | `/entra/login`、`/entra/china/login` | GET | 发起全球版或世纪互联登录。 |
| Entra | `/entra/callback`、`/entra/china/callback` | GET/POST | OAuth 回调处理。 |
| Entra | `/entra/logout`、`/entra/auth-status`、`/entra/photo` | GET/POST | 登录态、登出、用户头像。 |
| 管理后台 | `/admin` | GET/POST | 管理后台入口。 |
| 管理后台 | `/admin/faq...` | GET/POST | FAQ 管理与内容导入。 |
| 管理后台 | `/admin/accounts...` | GET/POST/PUT/DELETE | 管理员账号、密码与 MFA 管理。 |
| 管理后台 | `/admin/logs`、`/admin/webchat-access` | GET/POST | 查看问题日志、审核用户访问授权。 |

完整的路由定义以 [start_server.py](start_server.py) 中的 `create_application` 为准。

## 环境要求

- Python 3.11 或更高版本。
- 支持创建虚拟环境的 Python 安装。
- 访问实际 Copilot Studio、Direct Line、Entra ID 或 Graph 时，需要对应网络连通性和已授权的应用注册。
- 运行 Teams App Tester 时，需要 Node.js 与 npm。
- 生产环境需要可持久化保存 `data/`、提供 HTTPS 反向代理并妥善管理机密。

## 环境变量

应用启动时会加载 `.env`。不要提交实际 `.env`、Bearer Token、客户端密钥或生产数据库。

| 分类 | 变量 | 必填条件 | 说明 |
| --- | --- | --- | --- |
| 聊天模式 | `WEBCHAT_BACKEND` | 否 | `local`（默认）、`directline`、`copilotstudio`。 |
| 聊天身份 | `WEBCHAT_USER_ID`、`WEBCHAT_USER_NAME` | 否 | 上游活动默认用户标识。 |
| Direct Line | `COPILOT_STUDIO_DIRECTLINE_TOKEN_ENDPOINT` | `directline` 模式必填 | Copilot Studio 移动应用渠道的 Token Endpoint。 |
| Conversations | `COPILOT_STUDIO_CONVERSATIONS_URL` | `copilotstudio` 模式必填 | Copilot Studio Conversations API 地址。 |
| 原始 Token | `COPILOT_STUDIO_TOKEN_MODE=raw`、`COPILOT_STUDIO_BEARER_TOKEN` | raw 模式必填 | 需提供有效 JWT。 |
| 客户端凭据 | `COPILOT_STUDIO_TOKEN_MODE=aad_client_credentials`、`AAD_TENANT_ID`、`AAD_CLIENT_ID`、`AAD_CLIENT_SECRET` | 客户端凭据模式必填 | `AAD_SCOPE` 为空时，服务按 Conversations 主机推导。 |
| 管理员 | `FAQ_ADMIN_USERNAME`、`FAQ_ADMIN_PASSWORD` | 首次创建管理员时密码必填 | 用户名默认 `admin`；初始账号仅在表为空时创建。 |
| 管理后台安全 | `FAQ_SESSION_SECRET` | 生产强烈建议 | Cookie HMAC 签名密钥，建议使用至少 32 位随机字符。 |
| 日志保留 | `FAQ_LOG_RETENTION_DAYS` | 否 | 问题日志保留天数，默认 30。 |
| 全球 Entra | `ENTRA_CLIENT_ID`、`ENTRA_CLIENT_SECRET` | 启用全球 Entra 时必填 | 两项同时存在才启用。 |
| 全球 Entra | `ENTRA_AUTHORITY`、`ENTRA_REDIRECT_URI`、`ENTRA_SCOPES`、`ENTRA_SESSION_SECRET` | 否 | Authority 默认 `organizations`，生产建议显式指定回调地址和会话密钥。 |
| 世纪互联 Entra | `ENTRA_CHINA_CLIENT_ID`、`ENTRA_CHINA_CLIENT_SECRET` | 启用世纪互联时必填 | 两项同时存在才启用。 |
| 世纪互联 Entra | `ENTRA_CHINA_AUTHORITY`、`ENTRA_CHINA_REDIRECT_URI`、`ENTRA_CHINA_SCOPES` | 否 | 默认使用中国云 Authority 与 Microsoft Graph China 范围。 |

`.env.example` 是按运行模式、Copilot Studio 接入、FAQ 安全和 Entra ID 分组的变量参考，不应直接作为生产机密文件使用。

## 开发环境搭建与本地运行

### 1. 创建环境并安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2. 配置本地回显模式

复制环境变量示例，并至少配置初始管理员密码：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
WEBCHAT_BACKEND=local
FAQ_ADMIN_USERNAME=admin
FAQ_ADMIN_PASSWORD=<至少 8 位的本地开发密码>
FAQ_SESSION_SECRET=<至少 32 位随机字符串>
```

本地回显模式不需要 Copilot Studio、Entra ID 或 Direct Line 凭据。

### 3. 启动与访问

```powershell
python app.py
```

默认监听地址为 `http://0.0.0.0:5300`，可通过以下地址验证：

- 聊天页：`http://127.0.0.1:5300/webchat`
- 管理后台：`http://127.0.0.1:5300/admin`
- Agent 健康检查：`http://127.0.0.1:5300/api/messages`
- FAQ 目录：`http://127.0.0.1:5300/faq/directory`

FAQ 目录仅用于验证已发布条目的元数据返回。FAQ 详情需要启用 Entra ID，并使用已获批准的用户会话访问 `http://127.0.0.1:5300/faq/{faq_id}`；未启用 Entra ID 时，当前实现不提供匿名 FAQ 详情页。

PowerShell 健康检查示例：

```powershell
Invoke-WebRequest http://127.0.0.1:5300/api/messages
```

预期响应状态码为 `200`。

### 4. 本地调试与验证

```powershell
python -m unittest discover -s tests -v
python benchmarks\http_baseline.py --requests 500
```

执行基准前必须先启动应用。基准使用顺序请求，结果仅可用于回归比较，不代表生产并发容量。

如需使用 Microsoft 365 Agents Playground：

```powershell
npm install -g @microsoft/teams-app-test-tool
teamsapptester
```

测试工具使用的 Agent 地址为 `http://127.0.0.1:5300/api/messages`。

## Copilot Studio 接入

### Direct Line 匿名模式

在 Copilot Studio 中将目标 Agent 配置为允许匿名访问，并从“渠道 > 移动应用”获取 Token Endpoint。然后配置：

```dotenv
WEBCHAT_BACKEND=directline
COPILOT_STUDIO_DIRECTLINE_TOKEN_ENDPOINT=https://<token-endpoint>
```

启动应用后，通过 `/admin/connection-status` 检查本地配置是否完整；该接口不验证上游网络或凭据有效性。

### Conversations API 模式

使用原始 Bearer Token：

```dotenv
WEBCHAT_BACKEND=copilotstudio
COPILOT_STUDIO_CONVERSATIONS_URL=https://<host>/copilotstudio/.../conversations?api-version=2022-03-01-preview
COPILOT_STUDIO_TOKEN_MODE=raw
COPILOT_STUDIO_BEARER_TOKEN=<有效 JWT>
```

或使用 AAD 客户端凭据：

```dotenv
WEBCHAT_BACKEND=copilotstudio
COPILOT_STUDIO_CONVERSATIONS_URL=https://<host>/copilotstudio/.../conversations?api-version=2022-03-01-preview
COPILOT_STUDIO_TOKEN_MODE=aad_client_credentials
AAD_TENANT_ID=<租户 ID>
AAD_CLIENT_ID=<应用客户端 ID>
AAD_CLIENT_SECRET=<应用客户端密钥>
AAD_SCOPE=
```

`AAD_SCOPE` 为空时，服务尝试根据 Conversations API 主机推导 `<host>/.default`。匿名访问不要配置包含 `/authenticated/` 的 Conversations URL。

## 测试环境部署

测试环境建议使用独立的 `.env`、独立的 `data/` 目录副本以及测试专用 Copilot Studio/Entra 应用注册。

1. 将代码部署到目标主机，排除 `.env`、`.venv`、`__pycache__` 和本地调试文件。
2. 创建 Python 虚拟环境并安装 `requirements.txt`。
3. 通过部署平台的机密管理功能注入环境变量；不要将 `.env` 上传至版本库或镜像。
4. 初始化独立的 SQLite 数据库；设置 `FAQ_ADMIN_PASSWORD` 后首次启动会创建初始管理员。
5. 启动 `python app.py`，检查 `/api/messages`、`/webchat` 和 `/admin`。
6. 在实际配置的模式下分别验证建会话、发送、轮询、FAQ 目录与已批准用户的 FAQ 详情访问、后台登录和审批边界。
7. 执行 `python -m unittest discover -s tests -v`，并保存结果作为发布证据。

## 生产部署指南

### 部署前置要求

- 使用受支持的 Python 运行时、最小权限的运行账户和独立虚拟环境。
- 将 `FAQ_SESSION_SECRET`、`ENTRA_SESSION_SECRET`、客户端密钥和 Bearer Token 存入机密管理服务。
- 为 `data/faq.db`、`data/faq.db-wal`、`data/faq.db-shm` 与 `data/uploads/` 配置持久化卷和备份策略。
- 使用 HTTPS 反向代理或平台网关；Entra 的重定向地址必须与应用注册中登记的公网 HTTPS 地址一致。
- 防火墙仅开放反向代理所需端口；不要将 SQLite 数据库文件、`.env` 或上传目录公开暴露。
- 在上线前评估 CDN 可用性。若内网、离线或高可靠场景不允许依赖公网 CDN，应将前端资源本地化并调整模板引用。

### 标准操作步骤

1. 创建部署目录及受限权限的数据目录，恢复或初始化受控的 `data/` 数据。
2. 创建并激活虚拟环境，安装依赖：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install --requirement requirements.txt
   ```

3. 通过系统环境变量、容器 Secret 或平台配置注入生产参数；至少设置管理员初始化密码和高强度会话密钥。
4. 使用进程守护工具启动 `python app.py`，并将服务发布在反向代理之后。
5. 健康检查 `GET /api/messages` 必须返回 200；检查 `/webchat/config` 与 `/admin/connection-status` 的配置状态。
6. 从浏览器完成实际登录、FAQ 目录和获批用户的详情访问、WebChat 往返和附件上传的冒烟测试。
7. 对 `data/` 执行定期备份和恢复演练；升级前先备份数据库和上传文件。

### 生产运行注意事项

- 当前服务是单进程架构。不要在未改造共享会话、Token 缓存和 SQLite 写入策略的情况下直接使用多个 Worker 或横向扩容。
- 进程重启会清空管理员、Entra、Local Chat 与 Direct Line 内存会话；浏览器虽能恢复历史显示，但远端会话恢复依赖上游能力。
- `scripts/refresh-token-and-run.ps1` 面向本地委托 Token 刷新，会改写 `.env`，且其旧监听端口处理逻辑与当前 5300 端口不一致；不建议作为生产启动方案。
- 应用的 Secure Cookie 标记依赖请求 HTTPS 识别。部署在反向代理后时，应正确传递协议头并在上线前验证 Cookie 行为。

## 数据备份与恢复

- 备份对象：`data/faq.db`、同目录的 WAL/SHM 文件（存在时）以及 `data/uploads/`。
- 建议在低写入窗口或使用 SQLite 在线备份机制生成一致性副本，避免仅复制正在写入的主数据库文件。
- 恢复时停止应用，恢复数据库及附件目录后再启动；恢复完成后检查 FAQ 目录、附件链接和管理员登录。

## 维护与贡献规范

### 分支与提交

- 每项变更应使用独立分支，分支名称建议采用 `feature/`、`fix/`、`docs/`、`chore/` 前缀。
- 提交信息采用“类型: 简要说明”格式，例如 `feat: 增加 FAQ 分类筛选`、`fix: 修复 Direct Line Token 过期处理`。
- 单个提交应保持可构建、可测试，避免混入 `.env`、数据库、Token、调试日志和本地缓存。
- 修改接口、数据表、环境变量或安全策略时，必须同步更新本 README 与对应测试。

### 代码质量与测试

- 提交前至少执行：

  ```powershell
  python -m unittest discover -s tests -v
  python -m compileall -q app.py start_server.py faq.py
  ```

- 新增路由应补充成功、权限、参数校验和失败场景测试。
- 修改 FAQ 导入、上传或认证逻辑时，必须审查文件类型、大小限制、路径访问、CSRF 与授权边界。
- 涉及真实 Entra、Graph、Direct Line 或 Copilot Studio 的变更，需要在隔离测试租户完成集成验证；现有自动化测试不访问这些真实外部服务。

### 问题反馈与版本迭代

- 问题反馈应包含复现步骤、期望结果、实际结果、环境信息、脱敏日志和影响范围；不得粘贴 Token、密码、Cookie 或用户个人数据。
- 安全问题应通过受限渠道报告，不要在公开问题中披露漏洞利用细节或机密。
- 采用语义化版本规则：破坏性变更升级主版本，向后兼容的新功能升级次版本，兼容性修复升级修订版本。
- 发布前应完成回归测试、生产环境变量核对、数据备份、变更评审与回滚方案演练。

## 已知限制

- 默认 Agent 是回显示例，不含领域知识检索、模型推理、流式输出或取消上游推理能力。
- `/admin/connection-status` 只报告配置是否齐全，不能证明上游服务、Token 或网络可用。
- FAQ 目录当前不受 Entra ID 登录与审批控制；如 FAQ 标题和分类也属于受保护信息，应在后续版本为 `/faq/directory` 增加相同的授权校验。
- WebChat 移动端会隐藏聊天历史与 FAQ 侧栏，FAQ 详情、管理后台及登录/审批页面尚未提供与 WebChat 完全一致的手动主题切换能力。
- 服务没有内建容器化、CI/CD、TLS、监控告警和集中日志方案，需要由部署环境补充。
- `.env.example` 中的 `FAQ_TOTP_SECRET` 当前不参与源码配置读取；实际 MFA 密钥按管理员账号存储在数据库中。

## 目录说明

```text
.
├─ app.py                     Agent 启动入口与回显活动处理
├─ start_server.py            HTTP 路由、WebChat、Entra 与上游集成
├─ faq.py                     FAQ、SQLite、后台认证与管理能力
├─ templates/                 WebChat、登录页和管理后台模板
├─ tests/test_contracts.py    HTTP、权限、上传、FAQ 与审批回归测试
├─ benchmarks/                顺序 HTTP 基准工具
├─ scripts/                   本地辅助脚本
├─ data/                      SQLite 业务数据和上传文件，生产需持久化
├─ requirements.txt           Python 依赖清单
└─ .env.example               环境变量参考模板
```
