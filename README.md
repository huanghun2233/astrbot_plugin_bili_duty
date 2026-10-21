# AstrBot plugin: B站值班室（astrbot_plugin_bili_duty）

把 **「值班」式内容筛选** 和 **bili-cli 的取数/动作/护栏引擎** 合起来做成一个 AstrBot 插件：

- **值班日报**：每天定时挑几条视频，取字幕/章节/AI 摘要/热评，按 `### BV号` 的格式写成日报推给订阅的群；
- **常驻回复**：盯着「@我的 / 回复我的」，有人叫它就现写一句回过去（只做被动回应，默认预览、不真发）；
- **自带端口**：插件自己监听 `127.0.0.1:6198`，HTTP + WebSocket，别的程序能直接拉数据或推消息；
- **引擎不碰 cookie**：所有 B站 请求都走 `bili` CLI，限流、`audit.log`、风控熔断都在 CLI 里。

## 安装

1. 装引擎（需要一个能跑 `bili` 的 Python 环境）：

   ```bash
   uv tool install bili-agent --python 3.12
   bili auth login        # 扫码登录，凭证存在 %APPDATA%\bili-cli
   bili auth status       # 确认已登录
   ```

2. 用本仓库打包出的 zip 在 AstrBot 插件页上传安装（zip 根目录是 `astrbot_plugin_bili_duty/` 这一层，里面有 `metadata.yaml`）。

3. 重载插件，群里发 `bili 状态` 自检。

> 插件本身没有第三方依赖，`aiohttp` 由 AstrBot 自带。

## 指令

需要唤醒词（按你 AstrBot 的配置，例如 `肥鱼`）：

| 指令 | 作用 |
|---|---|
| `bili 状态` | 引擎路径/版本、B站登录、定时任务、API 端口、常驻状态 |
| `bili 日报 [条数]` | 立刻跑一轮，发摘要并写 `out/<date>.md` |
| `bili 热门 [条数]` | 综合热门榜（带链接） |
| `bili 视频 <BV号\|链接>` | 单条速览（字幕节选/章节/数据） |
| `bili 订阅` / `bili 取消订阅` | 把当前群/私聊加入每日日报名单 |
| `bili 评论 <BV号> <内容>` | 管理员；默认 `--dry-run`，`allow_comment` 打开才真发 |
| `bili presence` | 常驻状态；`预览 [n]` / `开` / `关` / `立刻` |

## 常驻：有人 @ 我、回我，就回一句

`presence.py` 每 90 秒看一次 `bili feed mentions` 和 `bili feed replies`：

- **只做被动回应**：别人 @ 我、回复我，我才接话；不主动刷评论区、不群发私信。
- **baseline**：第一次启动只把当时的通知 id 记进 `state.json`，历史积压一条都不回。
- **去重 + 频率**：回过的 id 记下来（重启也不重复回），每小时最多 `presence_max_per_hour` 条（默认 6），只回 `presence_max_age_hours`（默认 12 小时）内的。
- **默认预览**：`presence_dry_run: true` 时只把「本来要发什么」写进 `presence.log`，不真发；`bili presence 开` 或改配置才实发。
- **护栏在 CLI**：真发走 `bili comment reply`，由 bili-cli 的 guard（评论 10 秒间隔 / 30 条每小时）和 `audit.log` 兜底；被风控就停，不硬闯。

## 自带的 API 端口

插件会自己监听一个端口（默认 `127.0.0.1:6198`），其它程序可以像 SnowLuma 找 AstrBot 那样直接跟它说话，不用经过 QQ。

```bash
curl http://127.0.0.1:6198/health
curl http://127.0.0.1:6198/status
curl 'http://127.0.0.1:6198/hot?count=5'
curl 'http://127.0.0.1:6198/video?bvid=BV1Pyta66EDh'
curl 'http://127.0.0.1:6198/daily?count=2'
curl -X POST http://127.0.0.1:6198/notify \
     -H 'Content-Type: application/json' \
     -d '{"text":"日报跑完了","session":"qq-2056365224:GroupMessage:567845244"}'
```

统一信封（和 bili CLI 一致）：

```json
{"ok": true, "data": {"...": "..."}}
{"ok": false, "error": {"code": "busy", "message": "..."}}
```

WebSocket 在 `ws://127.0.0.1:6198/ws`，握手会发 `{"type":"hello","plugin":...,"actions":[...]}`，请求形如
`{"action":"presence","params":{"action":"preview","count":3},"echo":"x"}`，
回包带 `status` / `retcode` / `data`。

## 配置

见 `_conf_schema.json`。常用几项：

| 键 | 默认 | 说明 |
|---|---|---|
| `daily_enable` / `daily_time` | `true` / `21:30` | 每日日报开关与时间 |
| `daily_count` | `3` | 每天挑几条 |
| `min_duration` / `max_duration` | `120` / `2400` | 只挑这个时长区间的视频（秒） |
| `api_enable` / `api_port` | `true` / `6198` | 自带端口 |
| `presence_enable` | `true` | 常驻开关 |
| `presence_dry_run` | `true` | 只预览不真发 |
| `presence_max_per_hour` | `6` | 每小时最多回几条 |
| `presence_mode` | `llm` | `llm` 现写 / `template` 固定句式 |

## 设计取舍

- **为什么把 B站 访问全交给 CLI**：cookie、限流、审计、风控熔断只有一份实现，插件重启不影响登录态，出问题也能单独用 `bili` 复现。
- **为什么日报要写成 markdown 文件**：群里只发摘要，完整日报留在 `out/<date>.md`，方便事后翻。
- **为什么常驻默认 dry-run**：自动回复是写操作，先让人看几天「它本来会说什么」，再开实发。

## 致谢

- [kukemc/bili-cli](https://github.com/kukemc/bili-cli) —— 引擎与护栏
- [c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) —— 「值班」这套挑选/报告机制

## License

MIT

## v0.4.0 · 主动评论 + 动态记忆

- **主动评论**：值班挑到视频后，让模型先给视频打 1-10 的兴趣分，达到 `auto_comment_min_score`（默认 6）才写一句评论并到评论区发出去。
  - `auto_comment_enable`：总开关，默认关。
  - `auto_comment_dry_run`：默认开，只把评论写进活动日志，不真发；确认语气没问题后再关掉它。
  - `auto_comment_daily`：每天最多几条（默认 2），同一条视频不会重复评。
  - 走 `bili comment post`，因此受 bili-cli 的限流/审计约束。
- **动态记忆**：插件把每次真实动作写进 `journal.jsonl`（评论 / 回复 / 日报 / 投稿），聊天时自动把最近几条注入系统提示词，群里问「你刚在 B 站干嘛了」能如实回答。
  - `activity_prompt_items`：注入几条（0 = 关闭），`activity_prompt_hours`：只看最近多少小时。
  - 群里也能直接问：`bili 动态 [条数]`。
  - 模型另有 `bili_my_activity` 工具可主动查。
