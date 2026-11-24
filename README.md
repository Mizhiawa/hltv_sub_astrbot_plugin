# hltv_sub_astrbot_plugin
 version_2
移植自 [HakuchumuHYX/HakuBot](https://github.com/HakuchumuHYX/HakuBot) 中 `hltv_sub` 的 **HLTV (CS2) 大型赛事订阅与实时战报插件**，针对 **AstrBot 4.29.4 + SnowLuma 2.4.2 (OneBot v11)** 及 **Linux Ubuntu Docker** 部署环境重构与深度适配。

---

## ✨ 功能特性

- **自动发现与生命周期维护**：
  - 自动抓取 HLTV Big Events 并订阅，不错过近期大型赛事。
  - 赛事结束后保留宽限期补齐全部赛果，确认无遗漏后自动取消订阅并清理过期去重缓存。
  - 自适应轮询调度：无近期比赛时低频休眠，临近开赛、直播中或等待决赛 MVP 时自动切换至 3 分钟高频巡检。
- **全流程精美卡片主动推送**：
  - **开赛提醒**：包含对阵双方队标、赛制星级、总决赛/季军赛金色与铜色专属高亮、VRS 排名积分预测及赛事直邀概率（Invite Stakes）。
  - **非 BO1 比赛逐图播报（支持开关 `enable_map_result_push`）**：默认开启，BO3/BO5 系列赛每打完一张非决胜图立即推送该图半场阵营比分（CT/T 着色）与双方选手详细表现；**关闭后每打完一张地图不再播报（也不产生额外单图抓取请求），仅保留开赛提醒与整场结果推送**。
  - **全场终局战报**：自动校验结果页与详情页数据同步完整性（拒绝半残数据抢发），展示转播级比分条、VRS 赛后积分排名结算、地图池横幅、BP 顺序及仿 CS2 游戏内 TAB 计分板。
  - **赛事 MVP 海报**：总决赛结束后持续追踪赛事主页（最长 3 小时），第一时间推送 HLTV 官方 MVP 选手定妆照、奖章及与赛事均值对比柱状图。
- **Ubuntu Docker + SnowLuma 2.4.2 专项优化**：
  - **跨容器零路径依赖**：渲染图片统一以内存字节（`Comp.Image.fromBytes` / `base64://`）投递给 SnowLuma，无需在 `astrbot` 与 `snowluma` 容器间挂载共享临时图片目录。
  - **双渲染后端（`auto`）**：优先使用容器内 Playwright Chromium（自带 `--no-sandbox`、`--disable-dev-shm-usage` 防 64MB `/dev/shm` 崩溃、非 Headless UA 防队标拦截、2x 超清缩放及 15s 超时自动降级无图卡片）；若容器尚未安装 Chromium，自动无缝回退至 AstrBot 内置 `html_render` (T2I) 服务。
  - **内置数字窄体与中文字体自动发现**：`Barlow Condensed` 已内联编码；中文优先读取持久化数据目录内的 `MiSans`，并自动兼容 Ubuntu `fonts-noto-cjk` / `fonts-wqy-microhei` / `fonts-wqy-zenhei`。

---

## 🐳 Linux Ubuntu Docker 部署指南

### 1. 部署 FlareSolverr（必需）

HLTV 启用了 Cloudflare 防护，插件通过 **FlareSolverr** 获取页面 HTML 并持久复用会话。建议将 `flaresolverr` 与 `astrbot`、`snowluma` 放在同一 Docker 网络中：

```yaml
services:
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    restart: unless-stopped
    environment:
      - LOG_LEVEL=info
      - TZ=Asia/Shanghai
    # 若与 astrbot 在同一 compose 网络中无需暴露宿主机端口；若需宿主机访问可取消下方注释：
    # ports:
    #   - "127.0.0.1:8191:8191"
```

- **同 Docker Compose 网络**：插件配置中 `flaresolverr_url` 填写 `http://flaresolverr:8191/v1`（默认）。
- **宿主机桥接访问**：切勿填 `127.0.0.1`，请填写 Docker 网桥地址（通常为 `http://172.17.0.1:8191/v1`）。

### 2. 安装插件与依赖

在 AstrBot WebUI 插件市场通过仓库地址 `https://github.com/Mizhiawa/hltv_sub_astrbot_plugin` 安装，或在宿主机挂载的 `data/plugins/` 目录下克隆：

```bash
cd /path/to/astrbot/data/plugins
git clone https://github.com/Mizhiawa/hltv_sub_astrbot_plugin.git hltv_sub
```

随后在 `astrbot` 容器内确认 Python 依赖已安装：

```bash
docker exec -it astrbot pip install -r /AstrBot/data/plugins/hltv_sub/requirements.txt
```

### 3. （推荐）启用容器内 2x 本地超清 Playwright 渲染与中文字体

> 若跳过本步，`render_backend: auto` 会自动回退至 AstrBot 内置 `html_render` 服务，同样可直接出图。若追求与原版 HakuBot 完全一致的 2x 视网膜排版效果，推荐在容器内安装 Chromium 与中文字体，并将浏览器缓存持久化到 `/AstrBot/data` 卷中：

```bash
docker exec -it \
  -e PLAYWRIGHT_BROWSERS_PATH=/AstrBot/data/plugin_data/hltv_sub/playwright \
  astrbot bash -c "apt-get update && apt-get install -y fonts-noto-cjk fonts-wqy-microhei fontconfig && fc-cache -fv && playwright install --with-deps chromium"
```

（可选）如果你希望使用原版同款 **MiSans** 五字重字体，只需将 `MiSans-Regular.ttf`、`MiSans-Medium.ttf`、`MiSans-Semibold.ttf`、`MiSans-Bold.ttf`、`MiSans-Heavy.ttf` 放入宿主机挂载目录下的 `data/plugin_data/hltv_sub/fonts/`，插件会自动优先加载。

### 4. SnowLuma 2.4.2 对接注意事项

1. 在 SnowLuma 2.4.2 WebUI 中新建 **OneBot v11 反向 WebSocket** 连接，目标地址指向 AstrBot 的 `aiocqhttp` 适配器（例如 `ws://astrbot:6199/ws`）。
2. 在目标 QQ 群中由群主、群管理员或 AstrBot 管理员发送 `hltv开启`（或 `/hltv开启`），插件会自动记录该群的 `unified_msg_origin` 并激活赛事巡检。
3. 历史 `subscriptions.json`（无论来自 HakuBot 原版还是旧版 AstrBot 插件）直接位于 `data/plugin_data/hltv_sub/subscriptions.json` 时均可无缝升级兼容。

---

## ⚙️ 主要配置项说明

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `flaresolverr_url` | `http://flaresolverr:8191/v1` | FlareSolverr v1 接口地址 |
| `flaresolverr_timeout_seconds` | `60` | FlareSolverr 单次页面超时时间（秒） |
| `enable_map_result_push` | `true` | **非 BO1 比赛逐图播报开关**。关闭后每打完一张地图不再播报，仅保留开赛提醒与整场结果推送 |
| `request_interval_seconds` | `0.0` | 串行请求最小间隔（秒） |
| `session_max_requests` | `30` | 单个 FlareSolverr 浏览器会话最大请求次数（到期自动轮换防内存增长） |
| `block_cooldown_seconds` | `600` | 被拦截后的初始冷却时间（秒） |
| `block_cooldown_max_seconds` | `7200` | 连续被拦截指数退避冷却上限（秒） |
| `timezone` | `Asia/Shanghai` | 赛事日期与比赛时间展示时区 |
| `auto_unsub_delay_days` | `2` | 赛事结束后延迟自动取消订阅天数 |
| `auto_sub_interval_days` | `7` | 自动扫描并订阅 HLTV Big Events 的间隔天数 |
| `notified_ttl_days` | `30` | 推送去重状态保留天数 |
| `render_backend` | `auto` | 渲染后端（`auto` / `playwright` / `astrbot_t2i`） |
| `allow_no_prefix` | `true` | 允许群内免 `/` 唤醒前缀直接输入命令触发 |
| `allow_group_admin` | `true` | 允许 QQ 群主与群管理员管理本群开关及赛事订阅 |
| `default_enabled_groups` | `[]` | 默认启用 HLTV 推送的群号列表 |
| `watermark_text` | `Designed by Hakuchumu\nModified by Mizhiawa` | 渲染卡片左下角水印文本 |

---

## 📖 指令一览

> 默认同时支持带唤醒前缀（如 `/events`）与免前缀直接发送（如 `events`）。

| 指令 | 别名 | 权限要求 | 说明 |
| :--- | :--- | :--- | :--- |
| `hltv开启` | `hltv启用`、`hltvon` | 群主 / 群管理员 / Bot 管理员 | 开启本群 HLTV 赛事主动推送与查询功能 |
| `hltv关闭` | `hltv禁用`、`hltvoff` | 群主 / 群管理员 / Bot 管理员 | 关闭本群 HLTV 功能 |
| `hltv帮助` | `hltv菜单`、`hltvhelp` | 所有群成员 | 查看本群开启状态、逐图播报状态、订阅数及指令说明 |
| `event列表` | `赛事列表`、`events` | 群成员（本群已开启） | 渲染 HLTV 正在进行及即将举行的 Big Events 卡片 |
| `event订阅 <赛事ID>` | `订阅赛事`、`subscribe` | 群主 / 群管理员 / Bot 管理员 | 手动订阅指定赛事（如 `event订阅 7148`） |
| `event取消订阅 <赛事ID>` | `取消订阅赛事`、`unsubscribe` | 群主 / 群管理员 / Bot 管理员 | 取消订阅指定赛事 |
| `我的订阅` | `订阅列表`、`mysub` | 群成员（本群已开启） | 查看当前已订阅的所有赛事及举办日期 |
| `matches列表` | `比赛列表`、`matches` | 群成员（本群已开启） | 渲染已订阅活跃赛事的直播与待开始赛程图 |
| `results列表` | `结果列表`、`results` | 群成员（本群已开启） | 渲染已订阅活跃赛事的近期完赛比分图 |
| `stats [比赛ID]` | `比赛数据`、`数据` | 群成员（本群已开启） | 渲染指定比赛 ID（或最近完赛）的详细战报数据图 |

---

## 📄 开源许可与致谢

- 原插件设计与核心逻辑作者：**[Hakuchumu](https://github.com/HakuchumuHYX/HakuBot)**（MIT License）
- AstrBot 移植维护：**[Mizhiawa](https://github.com/Mizhiawa/hltv_sub_astrbot_plugin)**
- 内置数字字体：**Barlow Condensed**（SIL Open Font License 1.1，详见 [OFL.txt](./templates/fonts/OFL.txt)）
