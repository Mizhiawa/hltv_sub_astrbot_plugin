# HLTV订阅（AstrBot 适配版）
https://github.com/HakuchumuHYX/HakuBot/tree/main
原 NoneBot2 插件 [hltv_sub] 的由 Mizhiawa 修改的 AstrBot 移植版，**功能保持不变**：
HLTV CS2 赛事订阅、比赛/结果/数据查询、开赛提醒与结果推送（含单图结果推送）。

## 命令

与原版一致（无前缀、无需唤醒词，群内直接输入即可触发）：

| 命令 | 说明 |
| --- | --- |
| `event列表` / `赛事列表` / `events` | 查看近期大型赛事 |
| `event订阅 [ID]` / `订阅赛事 [ID]` / `subscribe [ID]` | 订阅赛事（需群主/管理员） |
| `event取消订阅 [ID]` / `取消订阅赛事 [ID]` / `unsubscribe [ID]` | 取消订阅（需群主/管理员） |
| `我的订阅` / `订阅列表` / `mysub` | 查看已订阅赛事 |
| `matches列表` / `比赛列表` / `matches` | 已订阅赛事的比赛列表（图片） |
| `results列表` / `结果列表` / `results` | 已订阅赛事的最近结果（图片） |
| `stats [match_id]` / `比赛数据 [match_id]` / `数据 [match_id]` | 比赛详细数据（图片） |
| `hltv开启` / `hltv启用` | 本群启用功能（需群主/管理员） |
| `hltv关闭` / `hltv禁用` | 本群禁用功能（需群主/管理员） |
| `hltv帮助` / `hltvhelp` | 帮助（图片） |
| `hltv_check` / `hltv_trigger` | 调试命令（仅超级用户） |

## 安装（详细步骤）

### 前提

- AstrBot v4.5+ 已部署并运行（本插件按当前 4.x API 编写）
- 已接入至少一个平台（推荐 OneBot v11 / `aiocqhttp` 适配器，如 NapCat、Lagrange、go-cqhttp 等）
- 可正常访问 `https://www.hltv.org`（HLTV 数据源；必要时配置代理）

### 第 1 步：放置插件目录

将本插件目录（含 `main.py`、`metadata.yaml`、`_conf_schema.json` 的整个 `hltv_sub` 文件夹）放到 AstrBot 的插件目录：

- **本机/虚拟环境直接运行**：`<AstrBot 项目根>/data/plugins/hltv_sub`
- **Docker 部署**：放到挂载进容器的 `data/plugins/` 目录下（即宿主机上
  `compose.yml` 中 `./data:/AstrBot/data` 映射的 `./data/plugins/`）

最终目录结构应为：

```
data/plugins/
└── hltv_sub/
    ├── main.py
    ├── metadata.yaml
    ├── _conf_schema.json
    ├── requirements.txt
    ├── handlers/  parsers/  scheduler_internal/
    ├── config.py  data_manager.py  data_source.py  http_client.py
    ├── image_utils.py  log_utils.py  models.py  permissions.py  render.py  scheduler.py
    └── __init__.py
```

### 第 2 步：安装依赖

在 AstrBot 所在 Python 环境中执行：

```bash
pip install -r <插件目录>/requirements.txt
# 等价于安装：curl_cffi httpx beautifulsoup4 lxml pytz Pillow apscheduler
```

若使用 AstrBot 的 Docker 镜像，依赖装在镜像内，需在容器里执行：

```bash
docker exec -it <astrbot容器名> pip install -r /AstrBot/data/plugins/hltv_sub/requirements.txt
```

> AstrBot WebUI 的插件管理页对部分插件支持自动安装依赖；若未自动安装，
> 请按上面命令手动安装。安装后重启 AstrBot。

### 第 3 步：安装中文字体（仅 Linux 服务器）

图片用 Pillow 绘制，**不需要浏览器**。但绘制中文需要系统中文字体，
否则图片里的中文会显示为方块：

```bash
# Debian / Ubuntu
apt install -y fonts-noto-cjk

# CentOS / RHEL / Fedora
yum install -y google-noto-sans-cjk-fonts   # 或 dnf install

# Alpine（Docker 常见）
apk add font-noto-cjk
```

Windows / macOS 自带中文字体，无需处理。

### 第 4 步：启用插件

1. 打开 AstrBot WebUI → 「插件管理」。
2. 确认列表中出现 `hltv_sub`（HLTV订阅），点击「启用」。
3. 若需要，点击「管理」→「重载插件」使其生效。

### 第 5 步：配置插件（可选）

在 WebUI 插件管理的配置弹窗中按需修改（默认值与原版一致）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `hltv_timezone` | `Asia/Shanghai` | 比赛时间显示时区 |
| `hltv_timeout` | `15` | 请求超时（秒） |
| `hltv_min_delay` | `5.0` | 重试延迟基数（秒） |
| `hltv_scheduler_max_parallel` | `3` | 多赛事轮询并发上限 |
| `hltv_scheduler_jitter_seconds` | `8` | 轮询抖动（秒），防同刻并发 |
| `hltv_auto_unsub_delay_days` | `2` | 赛事结束后延迟自动退订天数 |
| `hltv_notified_ttl_days` | `30` | 推送去重状态保留天数 |
| `hltv_watermark_text` | `Designed by Hakuchumu\nModified by M1z` | 图片水印 |
| `hltv_proxy_list` | `[]` | 代理列表，如 `["http://127.0.0.1:7890"]` |
| `hltv_impersonate` | `chrome124` | curl_cffi 浏览器指纹 |
| `hltv_flaresolverr_url` | 空 | FlareSolverr 地址（Cloudflare 回退，可选） |
| `hltv_superusers` | `[]` | 插件级超级用户 ID（调试命令用；留空回退 AstrBot `admins_id`） |
| `hltv_enable_map_result_push` | `true` | 非 BO1 比赛逐图播报开关。关闭后每打完一张地图不再播报，仅保留开赛提醒与整场结果推送 |

### 第 6 步：验证

在机器人所在的 QQ 群里（建议先在测试群）：

1. 群主/管理员发送 `hltv开启` → 回复「✅ HLTV 订阅功能已开启」
2. 发送 `event列表` → 收到赛事列表图片
3. 发送 `event订阅 7148`（替换为列表中的真实 ID）→ 订阅成功
4. 发送 `hltv帮助` → 收到帮助图片
5. 关注后续开赛提醒 / 结果推送是否到达

> 命令无需 `/` 前缀、无需 @ 机器人，群内直接输入即可（与原版一致）。

### 数据迁移（可选，从 NoneBot 版迁移时）

原版的订阅数据在 NoneBot localstore 数据目录下的
`<插件数据目录>/subscriptions.json`，直接拷贝到
`data/plugin_data/hltv_sub/subscriptions.json` 即可，文件格式兼容
（原版群记录无 `platform_id` 字段，插件会回退到第一个 aiocqhttp 平台推送）。

## 与原版的差异（仅平台适配部分）

- 消息平台：原版仅支持 OneBot v11（onebot.v11），本版面向 AstrBot 的
  `aiocqhttp` 适配器（`metadata.yaml` 的 `support_platforms`），
  权限判定、图片发送、主动推送均通过 AstrBot API 实现。
- 超级用户：原版读取 NoneBot 的 `superusers`；本版读取插件配置
  `hltv_superusers`，未配置时回退到 AstrBot 全局 `admins_id`。
- 图片渲染：原版使用 HTML 模板 + 无头浏览器截图；本版改用 Pillow 纯
  Python 绘制（信息内容等价，视觉风格沿用深色主题），避免对浏览器的依赖。
- 数据目录：`data/plugin_data/hltv_sub/subscriptions.json`
  （原版为 NoneBot localstore 目录，需要迁移数据时手动拷贝该文件即可，
  文件格式兼容）。
- 定时任务：由内置 apscheduler 驱动（不再依赖 nonebot_plugin_apscheduler）。

## 许可证

本插件为原 [HakuBot](https://github.com/Hakuchumu/HakuBot) 项目
`plugins/hltv_sub` 的移植，遵守原项目的开源许可协议。
