# tgauto

Telegram 签到 Web 控制台。Docker 镜像由 GitHub Actions 自动构建并发布到公开的 GHCR，服务器无需安装 Python 或本地编译。

## 部署

### 1. 准备 Docker

服务器安装 Docker Engine 和 Docker Compose Plugin。

### 2. 获取项目并配置

```bash
git clone https://github.com/Bibibiibi/tgauto.git
cd tgauto
cp .env.example .env
mkdir -p data
```

编辑 `.env`，至少填写 Telegram 的 `TG_API_ID` 和 `TG_API_HASH`。凭据从 [my.telegram.org](https://my.telegram.org) 的 **API development tools** 获取。

如需为管理页面启用登录保护，同时设置 `WEB_ADMIN_USER` 和 `WEB_ADMIN_PASSWORD`。

如需通过文件配置签到任务，先执行 `cp bots.json data/bots.json` 后再编辑；也可以启动后在 Web 控制台中配置。

### 3. 启动服务

```bash
docker compose up -d
```

浏览器访问 `http://服务器IP:8080`。

首次使用时，在 Web 控制台的“Telegram API 配置”中确认配置，然后点击“开始签到”并按页面提示完成验证码和 2FA 登录。登录会话及所有运行数据保存在 `./data`，重启或更新不会丢失。

### 4. 更新版本

GitHub Actions 会在 `main` 分支更新后自动发布新镜像。服务器执行：

```bash
docker compose pull
docker compose up -d
```

### 5. 常用命令

```bash
# 查看服务状态
docker compose ps

# 查看日志
docker compose logs -f tg-checkin

# 停止服务
docker compose down
```

## 注意事项

- `./data` 包含 Telegram 登录会话、API 配置和签到数据，请妥善保护，不要提交到 Git。
- Web 控制台默认监听 `8080`。建议设置 `WEB_ADMIN_PASSWORD`；公网部署还应使用 HTTPS 反向代理。
- `docker-compose.yml` 同时启动 `tg-checkin` 和 Mihomo。代理订阅可在 Web 控制台的“订阅”页面配置。
