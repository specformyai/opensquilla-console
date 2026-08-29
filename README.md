# Squilla Console

一个给 [OpenSquilla](https://github.com/opensquilla/opensquilla) 网关用的单文件运维控制台：在浏览器里对话测活、管理多个上游凭据、切换模型、查用量与路由决策，并直接安装/升级 OpenSquilla 本体。

后端是一个 FastAPI 应用，前端是无构建步骤的原生 HTML/CSS/JS（不需要 npm、不需要打包）。部署产物就是这个目录本身。

## 它解决什么

OpenSquilla 网关本身没有图形界面。日常运维要做的事——换一把上游 key、确认某个模型还活着、看看这次回复走了哪条路由、把网关升到新版本——都得敲命令或读日志。这个控制台把这些收进一个页面，并且在设计上假设**上游随时会挂**：任何一把凭据被限流或冻结，界面不会因此变空白。

## 功能

- **对话测活**：直连网关的流式对话，带思考过程与阶段展示，用固定探针问题快速判断链路是否通。
- **多凭据管理**：增删改、逐把测活、查余额、一键切换激活凭据。凭据存在 `data/keys.json`（0600），不入库。
- **模型目录**：四路数据源自动获取（网关 `models.list`、网关 onboarding 发现、上游 `/v1/models`、磁盘缓存），落盘缓存 1 小时 TTL、失败 5 分钟负缓存、30 分钟后台刷新。**单把凭据被冻结不会清空目录**——这是它和"直接打一次接口"的关键区别。
- **安装 / 升级 OpenSquilla**：读当前版本、列出所有带 wheel 资产的已发布版本、预检（uv 可用性、磁盘、目标 wheel 存在）、停网关 → 替换 tool 环境 → 起网关 → `/healthz` 复验，全过程流式日志。也可以选任意历史版本回退。
- **强制改密**：首次登录必须修改初始密码，否则服务端对所有业务接口返回 403、WebSocket 直接拒连。
- **用量与路由**：token 用量统计，以及每次回复的路由决策过程（五阶段 trail + 打分）。
- **深色沉浸式界面**：Lucide 图标，移动端响应式（侧栏折叠为底部 tab 栏），无 emoji。

## 快速开始

```bash
git clone https://github.com/specformyai/opensquilla-console.git
cd opensquilla-console

python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx pydantic websockets

# 网关地址按需覆盖，默认 127.0.0.1:18791
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8799
```

打开 `http://127.0.0.1:8799`。首启会自动生成初始密码并写入 `data/bootstrap-password.txt`（0600），登录后会被强制要求修改，改完该文件自动删除。

配置项全部可选，清单和说明见 [`.env.example`](.env.example)。

### 安全提醒

控制台自带登录鉴权，并且**没有"关掉鉴权"这个选项**：凭据库首启即初始化，`data/auth.json` 永远存在。但它默认监听 `127.0.0.1`，也没有 HTTPS、没有速率限制。要从公网访问，请放在反向代理后面并配好 TLS，不要直接把端口暴露出去。

## 运行为服务

```ini
# /etc/systemd/system/squilla-console.service
[Unit]
Description=Squilla Console
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/squilla-console
Environment=SQUILLA_CONSOLE_DATA=/var/lib/squilla-console/data
Environment=SQUILLA_GATEWAY_WS=ws://127.0.0.1:18791/ws
Environment=SQUILLA_GATEWAY_HTTP=http://127.0.0.1:18791
ExecStart=/opt/squilla-console/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8799
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

若要用安装/升级功能，还得让控制台看得见 `uv` 和 `opensquilla`。它们通常不在 systemd 的默认 PATH 上：

```ini
Environment=PATH=/opt/opensquilla/uv-bin:/usr/local/bin:/usr/bin:/bin
Environment=SQUILLA_UV_BIN=/opt/opensquilla/uv-bin/uv
Environment=SQUILLA_OS_BIN=/opt/opensquilla/uv-bin/opensquilla
Environment=UV_TOOL_DIR=/opt/opensquilla/uv-tools
Environment=UV_TOOL_BIN_DIR=/opt/opensquilla/uv-bin
Environment=UV_CACHE_DIR=/opt/opensquilla/uv-cache
Environment=UV_PYTHON_INSTALL_DIR=/opt/opensquilla/uv-python
```

## 项目结构

```
app.py              FastAPI 应用：路由、鉴权、网关链路、凭据库
authstore.py        操作员凭据存储：PBKDF2、强制改密、改密即失效旧会话
installer.py        OpenSquilla 安装/升级驱动：版本发现、预检、流式安装
model_catalog.py    模型目录：多源获取 + 磁盘缓存 + TTL/负缓存
static/             前端（无构建步骤）
tests/test_backend.py  端到端后端测试：全新安装 → 强制改密 → 闸门验证
```

## 几个设计决定

**升级只走 GitHub Releases 的 wheel，不走 PyPI。** PyPI 上的 `opensquilla` 落后实际发布多个小版本，把"最新版"解析成 PyPI 最新会导致降级。代码里有一条硬闸门确保候选版本必须真的比已装版本新，且 wheel 资产必须已发布。

**版本查询用两个独立信源。** `opensquilla version --check --json` 读官方渠道清单（阿里云 OSS 镜像，国内可达、无 API 限流），GitHub Releases API 提供权威的资产列表。清单跑在资产发布之前时，以资产列表为准，避免装到一个 404。

**wheel URL 永不接受客户端输入。** 浏览器只能提交版本号，服务端校验后自己拼出规范 URL。接受 URL 会把运维面板变成任意代码安装器。

**升级前必须停网关。** tool 环境被替换后，还在跑的旧进程会挂在已删除的 inode 上——它能继续工作到下次重启，这会让一次失败的升级在几个小时内看起来是成功的。

**强制改密的闸门在服务端，不在前端。** 中间件与 WebSocket 握手各有一道；前端遮罩只是界面，curl 直连一样被 403 拦住。

**密码策略写死在代码里，不可配置。** PBKDF2-HMAC-SHA256、210,000 次迭代、每账户独立随机 salt；新密码至少 12 位，且拒绝包含项目名、`admin`、`password` 一类可猜片段。会话密钥由密码派生，因此改密会立即失效所有设备上的旧 cookie，不需要服务端会话表。

**改密不需要服务端会话表。** 会话签名密钥从密码派生，改密后所有旧 cookie 自动失效。

## 测试

```bash
.venv/bin/python tests/test_backend.py
```

真实 ASGI 端到端：全新安装 → 用初始密码登录 → 确认所有业务接口被 403 拦住 → 强制改密 → 确认恢复可用。

## License

MIT，见 [LICENSE](LICENSE)。
