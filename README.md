# Telegram 机器人签到示例

这个示例使用个人 Telegram 账号的 MTProto API，通过 Telethon 逐个给机器人发送命令并读取回复。签到命令和按钮文字放在 `bots.json`，不需要改代码。

## 1. 准备 API 凭据

用要执行签到的 Telegram 账号登录 [my.telegram.org](https://my.telegram.org)，进入 **API development tools** 创建应用，得到 `api_id` 和 `api_hash`。`api_hash` 是密钥，不要提交到 Git 或发给别人。

注意：这里登录的是个人账号，不是目标机器人的 token。Bot API 的 bot 账号不能代替用户身份完成这类签到。

## 2. 安装和配置

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

将 `.env` 中的值导出到当前 shell（或使用你自己的进程管理器注入环境变量）：

```bash
set -a
source .env
set +a
```

编辑 `bots.json`，把真实机器人用户名和命令填进去，并将要执行的项设为 `"enabled": true`。示例中的占位任务默认关闭。每一项支持：

* `bot`：用户名，例如 `@foo_bot`
* `command`：发送的文字，例如 `/checkin`、`/start`
* `button`：可选，收到带键盘的回复后点击该文字
* `timeout`：等待回复的秒数
* `click_wait`：点击按钮后可选的后续等待时间；填 `0` 表示不等待
* `pause_after`：与下一个机器人之间的间隔秒数
* `enabled`：是否执行

先验证配置，不会发消息：

```bash
python tg_checkin.py --dry-run
```

## 3. 首次运行和定时运行

首次正式运行：

```bash
python tg_checkin.py
```

终端会要求输入 Telegram 登录验证码；如果账号开启了两步验证，还会要求输入 2FA 密码。成功后会生成 `tg_checkin.session`。这个文件等同于登录凭据，必须妥善保管，已经加入 `.gitignore`。之后运行时直接复用 session，不会重复要验证码。

之后可用 cron 或系统任务调度器执行，例如每天 08:00：

```cron
0 8 * * * cd /path/to/tgauto && . .venv/bin/activate && set -a && . .env && set +a && python tg_checkin.py >> checkin.log 2>&1
```

## 4. 按机器人定制流程

不同机器人可能要求先加入频道、发送验证码、回答问题或完成 CAPTCHA。这个模板只处理“发送文字 -> 等待回复 -> 可选点击按钮”；不要尝试绕过 CAPTCHA 或机器人的风控。需要多步流程时，在 `run_task` 中为该机器人增加明确的步骤，并保持每一步都有超时。

Telegram 可能返回 `FloodWaitError`。脚本会记录错误并继续处理其他机器人；应降低频率，不要在错误时循环狂发请求。

## 5. Docker 内网 Web 控制台

项目包含一个 FastAPI Web 控制台，提供首页概览、任务配置、保存、手动签到、每日自动签到、运行状态和日志查看。首页会记录每个机器人最近一次签到状态和从回复中解析出的本次积分；解析不到积分时显示为空。自动签到使用 `Asia/Shanghai` 时区，每天在 `00:00` 到 `00:20` 之间随机执行一次启用的任务；执行日期保存在 `./data/schedule.json`，签到历史保存在 `./data/checkin_history.json`，服务重启不会在同一天重复执行。配置和 session 保存在宿主机的 `./data` 目录中。

侧边栏的“订阅”页面使用官方 Mihomo 容器作为 Telegram 专用代理内核。输入 HTTP/HTTPS 订阅链接后，Mihomo 会以 `proxy-providers` 方式拉取并解析节点，页面可以刷新节点、查看延迟和选择 `TG-Proxy` 代理组。启用后，签到脚本和 Web 签到任务会通过 Mihomo 的 SOCKS5 端口连接 Telegram；其他宿主机流量不会被接管。订阅配置保存在 `./data/proxy_settings.json`，生成的 Mihomo 配置和节点缓存保存在 `./data/mihomo/`。

Web 控制台也可以在“Telegram API 配置”区域保存 `API ID`、`API Hash` 和手机号，设置保存在 `./data/settings.json`。API Hash 不会通过读取接口返回；留空后保存表示保留已保存的 Hash。首次点击 Web 的“开始签到”时，如果还没有登录 session，页面会出现验证码输入框；开启两步验证时还会继续要求输入 2FA 密码。命令行交互式登录仍可用，并会优先读取 Web 保存的设置，环境变量仍可覆盖它们。

控制台“网站签到”当前内置 NodeSeek 签到：登录地址、签到请求头和成功判定均由服务端固定，页面可选择固定签到（`POST /api/attendance?random=false`）或随机签到（`POST /api/attendance?random=true`），并管理 Cookie、启用状态和每日执行时间。可以直接粘贴已有 Cookie，或通过账号密码获取 Cookie。账号和密码仅在一次登录任务的内存中使用，成功后只保存 Cookie，不写入配置、历史或日志，接口和页面也不会回显 Cookie。

NodeSeek 账号密码登录会直接请求官方登录接口，账号和密码仅在一次登录任务的内存中使用，成功后只保存 Cookie，不写入配置、历史或日志，接口和页面也不会回显账号密码。Cookie 保存在 `./data/website_checkin.json`，文件会设置为仅所有者可读写。签到结果和自动调度状态分别保存在 `./data/website_checkin_history.json`、`./data/website_schedule.json`。

先准备环境变量和目录。GitHub Actions 会在推送 `main` 后自动构建并发布
`ghcr.io/bibibiibi/tgauto:latest`，服务器不需要安装 Python 或在本地编译：

```bash
cp .env.example .env
mkdir -p data
docker compose up -d
```

如果这个 GHCR 镜像被设置为私有，先登录一次（公开镜像不需要这一步）：

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u GITHUB_USERNAME --password-stdin
```

之后更新版本时执行：

```bash
docker compose pull
docker compose up -d
```

浏览器打开 `http://宿主机内网IP:8080`。如果不想在 Web 页面输入验证码，也可以先在容器里完成一次交互式登录：

```bash
docker compose run --rm -it tg-checkin python tg_checkin.py
```

登录成功后，session 会落在 `./data/tg_checkin.session`，配置会落在 `./data/bots.json`，再启动 Web 服务即可：

```bash
docker compose up -d
```

Web 服务默认监听 `0.0.0.0:8080`，这是为了让同一内网的其他设备访问。请在宿主机防火墙或路由器上只允许可信内网网段访问 8080，不要直接暴露到公网；当前页面没有用户认证。也可以在 `docker-compose.yml` 中改成 `127.0.0.1:8080:8080`，只允许本机访问。

## 6. 专用测试服务器

当前专用测试服务器为 `root@192.168.50.109`，仅用于内网部署和验证。登录凭据保存在本机的 `.test-server.md`，该文件已加入 `.gitignore`，不会进入版本记录或 Docker 构建上下文。
