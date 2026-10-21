"""B站值班室 — an AstrBot front-end for the bili-cli engine.

Design borrowed from dsh-bili-scroll (the "值班" idea: a scheduled sweep that
picks a few videos, digests them and reports back), engineered as an AstrBot
plugin: all B站 access goes through the ``bili`` CLI, so credentials, rate
limits and the audit log stay in one place.

Besides the chat side it also runs its own loopback API port (default
``127.0.0.1:6198``, ``duty_api.py``) so other programs can pull the same data
over HTTP or WebSocket — the plugin is both a client of AstrBot and a small
service other tools can talk to.

Commands (wake prefix applies, e.g. ``肥鱼 bili 日报``):

    bili 状态              登录/引擎/定时任务/API 端口自检
    bili 日报 [条数]       现在就跑一轮，发送摘要并写 out/<date>.md
    bili 热门 [条数]       综合热门榜（带链接）
    bili 视频 <BV号>       单条视频速览（字幕/章节/数据）
    bili 订阅 | 取消订阅   把当前会话加入/移出每日日报名单
    bili 评论 <BV号> <内容> 管理员：走 bili-cli guard 发一条评论
    bili api               查看 HTTP/WebSocket API 端口与用法
    bili presence          常驻：别人 @ 我、回我时自动回一句（默认 dry-run）
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from .bili_engine import BiliError, resolve_cli, run_bili, version
from .duty_api import ApiError, DutyApiServer
from .duty_core import DutyEngine, StateStore, fmt_num, render_qq_message
from .journal import ActivityJournal
from .presence import PresenceWatcher

PLUGIN_NAME = "astrbot_plugin_bili_duty"
PLUGIN_VERSION = "0.4.0"
HELP_TEXT = (
    "【B站值班室】指令：\n"
    "bili 状态 — 引擎/登录/定时/API 自检\n"
    "bili 日报 [条数] — 立刻跑一轮值班\n"
    "bili 热门 [条数] — 综合热门榜\n"
    "bili 视频 <BV号> — 单条速览\n"
    "bili 动态 [条数] — 我最近在 B 站做了什么（评论/回复/日报）\n"
    "bili 订阅 / bili 取消订阅 — 每日日报推送到本群\n"
    "bili 评论 <BV号> <内容> — 管理员，走 guard 限流\n"
    "bili api — 查看插件自己的 HTTP/WebSocket 端口\n"
    "bili presence — 常驻：有人 @ 我/回我时自动回一句（默认只预览，不真发）"
)


def _tool_event(context: Any) -> AstrMessageEvent | None:
    event = getattr(getattr(context, "context", None), "event", None)
    return event if isinstance(event, AstrMessageEvent) else None


class VideoDigestTool(FunctionTool):
    """让 LLM 能直接"看"一条 B站视频（字幕/章节/数据）。"""

    def __init__(self, plugin: "BiliDutyPlugin") -> None:
        super().__init__(
            name="bili_video_digest",
            description=(
                "查一条 B站视频的内容：标题、UP、播放/点赞、章节、字幕节选、链接。"
                "用户贴了 BV 号、av 号、b23.tv 或 bilibili.com 链接，或让你看看/总结某条视频时调用。"
                "参数可以是 BV 号，也可以直接是链接。返回的是字幕原文与元数据，不要编造字幕里没有的内容；"
                "没有字幕轨时会明确说明。回答用户时用自己的话概括，不要整段复述字幕。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "bvid": {
                        "type": "string",
                        "description": "BV 号（如 BV1Pyta66EDh）或 b23.tv / bilibili.com 视频链接。",
                    },
                },
                "required": ["bvid"],
                "additionalProperties": False,
            },
        )
        self._plugin = plugin

    async def call(self, context: Any, **kwargs: Any) -> str:
        return await self._plugin.tool_video_digest(str(kwargs.get("bvid") or ""))


class HotListTool(FunctionTool):
    """综合热门榜。"""

    def __init__(self, plugin: "BiliDutyPlugin") -> None:
        super().__init__(
            name="bili_hot_videos",
            description=(
                "取 B站综合热门榜（当前热度排序）。用户问“最近有什么火的/热门/大家在看的”时调用。"
                "返回若干条：标题、UP、播放量、链接。只报告返回的条目，不要补写没出现的视频。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "要几条，默认 5。",
                    },
                },
                "additionalProperties": False,
            },
        )
        self._plugin = plugin

    async def call(self, context: Any, **kwargs: Any) -> str:
        return await self._plugin.tool_hot(int(kwargs.get("count") or 5))


class DailyDutyTool(FunctionTool):
    """值班挑选：从推荐流里挑几条新的并给出速览。"""

    def __init__(self, plugin: "BiliDutyPlugin") -> None:
        super().__init__(
            name="bili_daily_pick",
            description=(
                "B站值班挑选：从首页推荐 + 热门 + 关键词里挑几条“没看过”的新视频，逐条取字幕速览。"
                "用户问“今天/最近有什么值得看的”“帮我挑几个视频”时调用。"
                "这个工具会花 10–30 秒，且会把挑中的视频记入已看列表（下次不会重复推）。"
                "count 默认 2、最多 3；用户没指定就别调太多次。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "description": "挑几条，默认 2。",
                    },
                },
                "additionalProperties": False,
            },
        )
        self._plugin = plugin

    async def call(self, context: Any, **kwargs: Any) -> str:
        return await self._plugin.tool_daily_pick(int(kwargs.get("count") or 2))


class ActivityTool(FunctionTool):
    """让我（bot）知道自己最近在 B 站干了什么。"""

    def __init__(self, plugin: "BiliDutyPlugin") -> None:
        super().__init__(
            name="bili_my_activity",
            description=(
                "查我自己最近在 B 站做过什么：发过哪些评论、回复过谁、跑过几次日报。"
                "用户问“你在B站干了什么/你刚评论了啥/你回过谁”时调用。"
                "参数 count 默认 5，最多 20。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "看最近几条，默认 5。",
                    },
                },
                "additionalProperties": False,
            },
        )
        self._plugin = plugin

    async def call(self, context: Any, **kwargs: Any) -> str:
        return self._plugin.activity_text(int(kwargs.get("count") or 5))


@register(PLUGIN_NAME, "大肥鱼值班室", "B站值班：每日挑片日报、单条速览、热门榜，并自带 HTTP/WS API 端口", PLUGIN_VERSION)
class BiliDutyPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.config: dict[str, Any] = dict(config or {})
        self._engine: DutyEngine | None = None
        self._store: StateStore | None = None
        self._data_dir = None
        self._cli: str | None = None
        self._task: asyncio.Task | None = None
        self._last_day: str | None = None
        self._running = False
        self._duty_lock = asyncio.Lock()
        self._api: DutyApiServer | None = None
        self._presence: PresenceWatcher | None = None
        self._presence_task: asyncio.Task | None = None
        self._journal: ActivityJournal | None = None
        self._comment_lock = asyncio.Lock()
        self._last_auto_comment: str = ""

    # ------------------------------------------------------------------ life
    async def initialize(self) -> None:
        cfg = self.config or {}
        self._cli = resolve_cli(str(cfg.get("cli_path") or "").strip())
        self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._store = StateStore(self._data_dir)
        self._journal = ActivityJournal(self._data_dir)
        logger.info(f"[bili-duty] journal ready: {len(self._journal.entries)} 条历史")

        if self._cli:
            keywords = [k.strip() for k in str(cfg.get("keywords") or "").split(",") if k.strip()]
            self._engine = DutyEngine(
                self._cli,
                min_duration=int(cfg.get("min_duration") or 120),
                max_duration=int(cfg.get("max_duration") or 2400),
                max_chars=int(cfg.get("max_transcript_chars") or 4000),
                keywords=keywords,
                logger=logger,
            )
            logger.info(f"[bili-duty] engine ready: {self._cli}")
            self.context.add_llm_tools(
                VideoDigestTool(self),
                HotListTool(self),
                DailyDutyTool(self),
                ActivityTool(self),
            )
            logger.info(
                "[bili-duty] llm tools: bili_video_digest, bili_hot_videos, "
                "bili_daily_pick, bili_my_activity"
            )
        else:
            logger.warning("[bili-duty] 没找到 bili 可执行文件，日报/视频指令会不可用")

        if bool(cfg.get("daily_enable", True)):
            self._task = asyncio.create_task(self._daily_loop(), name="bili-duty-daily")
            logger.info(f"[bili-duty] daily task started at {cfg.get('daily_time') or '21:30'}")

        await self._start_api()
        await self._start_presence()

    async def terminate(self) -> None:
        presence, self._presence = self._presence, None
        if presence is not None:
            try:
                await presence.stop()
            except Exception:  # noqa: BLE001
                pass
        ptask, self._presence_task = self._presence_task, None
        if ptask:
            ptask.cancel()
            try:
                await ptask
            except (asyncio.CancelledError, Exception):
                pass
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        api, self._api = self._api, None
        if api is not None:
            try:
                await api.stop()
                logger.info("[bili-duty] api server stopped")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili-duty] api server stop failed: {exc}")

    # ------------------------------------------------------------- api server
    async def _start_api(self) -> None:
        cfg = self._cfg()
        if not bool(cfg.get("api_enable", True)):
            logger.info("[bili-duty] api server disabled in config")
            return
        host = str(cfg.get("api_host") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(cfg.get("api_port") or 6198)
        except (TypeError, ValueError):
            port = 6198
        server = DutyApiServer(
            {
                "health": self._api_health,
                "status": self._api_status,
                "hot": self._api_hot,
                "video": self._api_video,
                "daily": self._api_daily,
                "notify": self._api_notify,
                "presence": self._api_presence,
            },
            host=host,
            port=port,
            token=str(cfg.get("api_token") or "").strip(),
            version=PLUGIN_VERSION,
            logger=logger,
        )
        try:
            await server.start()
        except OSError as exc:
            logger.error(f"[bili-duty] API 端口 {host}:{port} 启动失败：{exc}")
            return
        self._api = server

    # --------------------------------------------------------------- presence
    async def _start_presence(self) -> None:
        cfg = self._cfg()
        if not bool(cfg.get("presence_enable", True)):
            logger.info("[bili-duty] presence disabled in config")
            return
        if not self._engine or not self._store:
            logger.warning("[bili-duty] presence 需要 bili 引擎和状态文件，跳过")
            return
        kinds = tuple(
            part.strip()
            for part in str(cfg.get("presence_types") or "mentions,replies").split(",")
            if part.strip()
        )
        notify_session = str(cfg.get("presence_notify_session") or "").strip()
        watcher = PresenceWatcher(
            self._cli,
            self._store,
            interval=int(cfg.get("presence_interval") or 90),
            max_per_hour=int(cfg.get("presence_max_per_hour") or 6),
            max_age_hours=int(cfg.get("presence_max_age_hours") or 12),
            mode=str(cfg.get("presence_mode") or "llm"),
            template=str(cfg.get("presence_template") or "收到～"),
            dry_run=bool(cfg.get("presence_dry_run", True)),
            kinds=kinds,
            reply_fn=self._llm_reply,
            logger=logger,
            log_path=(self._data_dir / "presence.log") if self._data_dir else None,
            notify=self._presence_notify if notify_session else None,
            on_sent=self._on_presence_sent,
        )
        self._presence = watcher
        self._presence_task = asyncio.create_task(watcher.run(), name="bili-duty-presence")

    async def _llm_reply(self, prompt: str, item: dict[str, Any]) -> str:
        provider = None
        try:
            provider = self.context.get_using_provider()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili-duty] get_using_provider failed: {exc}")
        if provider is None:
            return ""
        resp = await provider.text_chat(
            prompt=prompt,
            session_id="bili-duty-presence",
            system_prompt=(
                "你在 B站 评论区回复别人。用中文，一句话到两句话，30 字以内，"
                "口气轻松自然，不要客服腔，不要自我介绍，不要提自己是 AI，不要刷表情。"
            ),
        )
        return str(getattr(resp, "completion_text", "") or "").strip()

    async def _presence_notify(self, text: str) -> None:
        session = str(self._cfg().get("presence_notify_session") or "").strip()
        if session:
            await self._send_to_session(session, text)

    # -------------------------------------------------------------- activity
    async def _on_presence_sent(self, item: dict[str, Any], text: str) -> None:
        """Record a real reply so the bot can talk about it later."""
        journal = self._journal
        if journal is None:
            return
        on = item.get("on") or {}
        journal.add(
            "reply",
            text,
            bvid=str(on.get("bvid") or ""),
            title=str(on.get("title") or ""),
            url=str(on.get("url") or ""),
            extra={"to": str((item.get("user") or {}).get("name") or "")},
        )

    def activity_text(self, n: int = 5) -> str:
        journal = self._journal
        if journal is None:
            return "活动日志还没初始化。"
        return journal.render(max(1, min(20, int(n))))

    def _note_daily(self, result: dict[str, Any]) -> None:
        journal = self._journal
        if journal is None:
            return
        videos = result.get("videos") or []
        if not videos:
            return
        titles = "；".join(str(v.get("title") or v.get("bvid")) for v in videos[:3])
        journal.add(
            "daily",
            f"挑了 {len(videos)} 条：{titles}",
            extra={"date": result.get("date"), "path": result.get("path")},
        )

    async def _llm_text(self, prompt: str, system: str) -> str:
        provider = None
        try:
            provider = self.context.get_using_provider()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili-duty] get_using_provider failed: {exc}")
        if provider is None:
            return ""
        resp = await provider.text_chat(
            prompt=prompt, session_id="bili-duty-comment", system_prompt=system
        )
        return str(getattr(resp, "completion_text", "") or "").strip()

    async def _comment_draft(self, video: dict[str, Any]) -> tuple[int, str]:
        """Ask the model: is this interesting enough, and what would we say?"""
        title = str(video.get("title") or "")
        owner = str(video.get("owner") or "")
        summary = str(video.get("ai_summary") or "").strip()
        transcript = str(video.get("transcript") or "").strip()
        tags = "、".join(str(t) for t in (video.get("tags") or [])[:6])
        hot = video.get("hot_comments") or []
        hot_text = " / ".join(
            str(c.get("content") or c.get("text") or "")[:40] for c in hot[:3] if isinstance(c, dict)
        )
        prompt = (
            f"视频标题：{title}\nUP主：{owner}\n标签：{tags}\n"
            f"AI 摘要：{summary[:300]}\n字幕节选：{transcript[:600]}\n"
            f"热评：{hot_text}\n\n"
            "先判断这条视频对一个喜欢「{kw}」的观众有多值得留言（1-10 分）；"
            "如果 >=6 分，写一条 15~40 字的评论：像真观众随口说的，可以接热评的梗，"
            "不要剧透关键结论，不要艾特人，不要放链接，不要自我介绍，不要表情包。\n"
            '只输出 JSON：{{"score": 7, "comment": "……"}}'
        ).format(kw=str(self._cfg().get("keywords") or "AI、编程、数码"))
        raw = await self._llm_text(
            prompt,
            "你是一个普通 B站 观众，正在刷视频。只输出 JSON，不要解释。",
        )
        if not raw:
            return 0, ""
        score = 0
        comment = ""
        try:
            start = raw.find("{")
            end = raw.rfind("}")
            import json as _json

            data = _json.loads(raw[start : end + 1]) if 0 <= start < end else {}
            score = int(data.get("score") or 0)
            comment = str(data.get("comment") or "").strip()
        except (ValueError, TypeError):
            logger.debug(f"[bili-duty] comment draft unparsable: {raw[:120]}")
            return 0, ""
        comment = comment.strip().strip('"').replace("\n", " ")
        return max(0, min(10, score)), comment[:120]

    async def _auto_comment(self, videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Comment on the picks the model finds interesting (guarded, capped)."""
        cfg = self._cfg()
        journal = self._journal
        if journal is None or not videos:
            return []
        if not bool(cfg.get("auto_comment_enable", False)):
            return []
        per_day = max(1, int(cfg.get("auto_comment_daily") or 2))
        min_score = max(1, min(10, int(cfg.get("auto_comment_min_score") or 6)))
        dry = bool(cfg.get("auto_comment_dry_run", True))
        posted: list[dict[str, Any]] = []
        if self._comment_lock.locked():
            return []
        async with self._comment_lock:
            for video in videos:
                if journal.count_today("comment") >= per_day:
                    break
                bvid = str(video.get("bvid") or "")
                if not bvid or journal.has_commented(bvid):
                    continue
                score, text = await self._comment_draft(video)
                if score < min_score or not text:
                    logger.info(f"[bili-duty] auto-comment skip {bvid} (score={score})")
                    continue
                data: dict[str, Any] = {"dry_run": dry}
                if not dry:
                    try:
                        data = await run_bili(
                            self._cli or "", "comment", "post", bvid, text,
                            "--type", "video", timeout=90,
                        )
                    except BiliError as exc:
                        logger.warning(f"[bili-duty] auto-comment {bvid} failed: {exc}")
                        continue
                journal.add(
                    "comment",
                    text,
                    bvid=bvid,
                    title=str(video.get("title") or ""),
                    url=str(video.get("url") or ""),
                    extra={"score": score, "dry_run": dry, "result": data},
                )
                posted.append({"bvid": bvid, "title": video.get("title"), "text": text, "score": score, "dry_run": dry})
                logger.info(f"[bili-duty] auto-comment {bvid} score={score} dry_run={dry}: {text}")
        return posted

    @filter.on_llm_request()
    async def inject_activity(self, event: AstrMessageEvent, req: Any) -> None:
        """Keep the bot aware of what it did on Bilibili, on every chat turn."""
        journal = self._journal
        if journal is None or req is None:
            return
        items = int(self._cfg().get("activity_prompt_items") or 0)
        if items <= 0:
            return
        try:
            block = journal.prompt_block(
                n=items,
                max_age_hours=int(self._cfg().get("activity_prompt_hours") or 24),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili-duty] activity block failed: {exc}")
            return
        if not block:
            return
        req.system_prompt = (req.system_prompt or "") + "\n\n" + block

    async def _api_presence(self, params: dict[str, Any]) -> dict[str, Any]:
        watcher = self._presence
        if watcher is None:
            return {"enabled": False, "reason": "presence 没启动（检查 presence_enable）"}
        todo = str(params.get("action") or params.get("todo") or "").strip()
        if todo in ("preview", "预览"):
            count = _int_param(params, "count", 3, 1, 10)
            return {"enabled": True, "dry_run": watcher.dry_run, "preview": await watcher.preview(count)}
        if todo in ("poll", "立刻"):
            return {"enabled": True, "dry_run": watcher.dry_run, "handled": await watcher.poll_once()}
        if todo in ("reply", "回"):
            target_id = str(params.get("id") or "").strip()
            if not target_id:
                raise ApiError("invalid_argument", "id 不能为空")
            return await self._api_presence_reply(
                watcher, target_id, str(params.get("text") or "").strip()
            )
        state = watcher._state()
        return {
            "enabled": True,
            "dry_run": watcher.dry_run,
            "interval": watcher.interval,
            "max_per_hour": watcher.max_per_hour,
            "bootstrapped": bool(state.get("bootstrapped")),
            "handled": len(state.get("handled") or {}),
            "last_poll": watcher.last_poll,
            "last_error": watcher.last_error,
        }

    async def _api_presence_reply(
        self, watcher: PresenceWatcher, item_id: str, text: str
    ) -> dict[str, Any]:
        items = await watcher.fetch()
        item = next((entry for entry in items if str(entry.get("id")) == item_id), None)
        if item is None:
            raise ApiError("not_found", f"通知列表里找不到 id={item_id}")
        composed = text or await watcher.compose(item)
        if not composed:
            raise ApiError("failed", "没生成出回复内容（模型可能不可用）")
        ok, detail = await watcher.post(item, composed)
        return {
            "enabled": True,
            "dry_run": watcher.dry_run,
            "ok": ok,
            "result": detail,
            "id": item_id,
            "user": (item.get("user") or {}).get("name"),
            "url": (item.get("on") or {}).get("url"),
            "text": composed,
        }

    async def _api_health(self, params: dict[str, Any]) -> dict[str, Any]:
        server = self._api
        return {
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "cli": self._cli,
            "engine": bool(self._engine),
            "host": server.host if server else None,
            "port": server.port if server else None,
            "connections": server._connections if server else 0,
            "uptime": round(_now_ts() - server.started_at, 1) if server else 0,
        }

    async def _api_status(self, params: dict[str, Any]) -> dict[str, Any]:
        engine = self._engine
        if engine is None:
            raise ApiError("engine_unavailable", "找不到 bili CLI，插件引擎不可用", status=503)
        auth = await engine.auth_status()
        return {
            "account": auth,
            "cli": self._cli,
            "seen": len(self._store.seen) if self._store else 0,
            "sessions": len(self._store.sessions) if self._store else 0,
            "last_run": self._store.last_run() if self._store else None,
            "api": {"host": self._api.host, "port": self._api.port} if self._api else None,
        }

    async def _api_hot(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._cli:
            raise ApiError("engine_unavailable", "找不到 bili CLI", status=503)
        count = _int_param(params, "count", 5, 1, 10)
        data = await run_bili(self._cli, "hot", "popular", "--size", str(max(count, 5)), timeout=60)
        items = []
        for item in (data.get("items") or [])[:count]:
            stat = item.get("stat") or {}
            items.append(
                {
                    "bvid": item.get("bvid"),
                    "title": item.get("title"),
                    "up": _up_name(item),
                    "duration": item.get("duration"),
                    "view": stat.get("view"),
                    "like": stat.get("like"),
                    "url": item.get("url"),
                }
            )
        return {"count": len(items), "items": items}

    async def _api_video(self, params: dict[str, Any]) -> dict[str, Any]:
        engine = self._engine
        if engine is None:
            raise ApiError("engine_unavailable", "找不到 bili CLI", status=503)
        bvid = _extract_bvid(str(params.get("bvid") or params.get("url") or ""))
        if not bvid:
            raise ApiError("invalid_bvid", "需要 bvid 或 bilibili.com / b23.tv 链接")
        return await engine.digest(bvid)

    async def _api_daily(self, params: dict[str, Any]) -> dict[str, Any]:
        count = _int_param(params, "count", int(self._cfg().get("daily_count") or 3), 1, 6)
        return await self._pick_daily(count)

    async def _api_notify(self, params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("text") or "").strip()
        session = str(params.get("session") or self._cfg().get("notify_session") or "").strip()
        if not text:
            raise ApiError("invalid_text", "text 不能为空")
        if not session:
            raise ApiError(
                "missing_session",
                "缺 session，例如 qq-2056365224:GroupMessage:567845244（也可在插件配置 notify_session 里写死）",
            )
        sent = await self._send_to_session(session, text)
        if not sent:
            raise ApiError("send_failed", f"没有找到匹配的平台，session={session}", status=502)
        return {"sent": True, "session": session, "chars": len(text)}

    # --------------------------------------------------------------- helpers
    def _cfg(self) -> dict:
        return self.config or {}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id())
        admins = {str(x) for x in (self._cfg().get("admin_ids") or [])}
        if sender in admins:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    async def _send(self, event: AstrMessageEvent, text: str) -> None:
        for chunk in _split(text, 1400):
            await event.send(MessageChain([Plain(chunk)]))

    async def _require_engine(self, event: AstrMessageEvent) -> DutyEngine | None:
        if self._engine:
            return self._engine
        await self._send(event, "没有找到 bili CLI。请在插件配置里填 cli_path，或先装 bili-cli。")
        return None

    # ------------------------------------------------------------- LLM tools
    async def tool_video_digest(self, raw: str) -> str:
        engine = self._engine
        if engine is None:
            return "bili 引擎不可用（找不到 bili 可执行文件）。"
        bvid = _extract_bvid(raw)
        if not bvid:
            return f"没从「{raw}」里认出 BV 号，请给出 BV 号或 bilibili.com / b23.tv 链接。"
        try:
            video = await engine.digest(bvid)
        except BiliError as exc:
            return f"取 {bvid} 失败：{exc}"
        stat = video.get("stat") or {}
        lines = [
            f"标题：{video.get('title')}",
            f"UP：{video.get('owner')}",
            f"数据：{fmt_num(stat.get('view'))} 播放 / {fmt_num(stat.get('like'))} 赞 / {fmt_num(stat.get('reply'))} 评论",
            f"时长：{_fmt_dur(video.get('duration'))}",
            f"链接：{video.get('url')}",
        ]
        chapters = [c.get("title") for c in (video.get("chapters") or []) if c.get("title")]
        if chapters:
            lines.append("章节：" + " | ".join(chapters[:8]))
        if video.get("ai_summary"):
            lines.append(f"AI 摘要：{video['ai_summary']}")
        transcript = (video.get("transcript") or "").strip()
        lines.append("字幕节选：" + (transcript[:900] if transcript else "（这条没有字幕轨，只有画面和元数据）"))
        return "\n".join(lines)

    async def tool_hot(self, count: int) -> str:
        if not self._cli:
            return "bili 引擎不可用（找不到 bili 可执行文件）。"
        count = max(1, min(10, count))
        try:
            data = await run_bili(self._cli, "hot", "popular", "--size", str(max(count, 5)), timeout=60)
        except BiliError as exc:
            return f"拉热门失败：{exc}"
        items = (data.get("items") or [])[:count]
        if not items:
            return "热门榜是空的。"
        lines = [f"B站综合热门（前 {len(items)} 条）："]
        for index, item in enumerate(items, start=1):
            stat = item.get("stat") or {}
            lines.append(
                f"{index}. {item.get('title')} — {_up_name(item)} · "
                f"{_fmt_dur(item.get('duration'))} · {fmt_num(stat.get('view'))} 播放 · "
                f"{item.get('url') or item.get('bvid')}"
            )
        return "\n".join(lines)

    async def _pick_daily(self, count: int) -> dict[str, Any]:
        engine = self._engine
        if engine is None:
            raise ApiError("engine_unavailable", "找不到 bili CLI，插件引擎不可用", status=503)
        count = max(1, min(6, int(count)))
        if self._duty_lock.locked():
            raise ApiError("busy", "上一轮值班还在跑，稍等几秒", status=429)
        async with self._duty_lock:
            result = await engine.daily(count, self._data_dir)
            self._note_daily(result)
            commented = await self._auto_comment(result.get("videos") or [])
            if commented:
                result["commented"] = commented
            return result

    async def _send_to_session(self, session: str, text: str) -> bool:
        """Push a plain-text message into any AstrBot session (umo string)."""
        sent = False
        for chunk in _split(text, 1400):
            try:
                ok = await self.context.send_message(session, MessageChain([Plain(chunk)]))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili-duty] notify send failed: {exc}")
                return False
            sent = sent or bool(ok)
        return sent

    async def tool_daily_pick(self, count: int) -> str:
        count = max(1, min(3, count))
        try:
            result = await self._pick_daily(count)
        except ApiError as exc:
            return exc.message
        except BiliError as exc:
            return f"值班挑选失败：{exc}"
        videos = result.get("videos") or []
        if not videos:
            return "今天没有挑到没看过的新视频。"
        lines = [f"今天挑到 {len(videos)} 条新视频："]
        for index, video in enumerate(videos, start=1):
            stat = video.get("stat") or {}
            lines.append(
                f"{index}. {video.get('title')} — {video.get('owner')} · "
                f"{_fmt_dur(video.get('duration'))} · {fmt_num(stat.get('view'))} 播放"
            )
            if video.get("ai_summary"):
                lines.append(f"   摘要：{video['ai_summary']}")
            transcript = (video.get("transcript") or "").strip()
            if transcript:
                lines.append(f"   字幕节选：{transcript[:400]}")
            lines.append(f"   链接：{video.get('url')}")
        lines.append("（完整日报已写入 out/<date>.md）")
        return "\n".join(lines)

    # ------------------------------------------------------------------ api cmd
    def _api_text(self) -> str:
        server = self._api
        if server is None:
            return "API 端口没起来（配置 api_enable / api_port，或端口被占用）。"
        host = "127.0.0.1" if server.host in ("0.0.0.0", "") else server.host
        base = f"http://{host}:{server.port}"
        token = "已启用（请求带 Authorization: Bearer …）" if server.token else "未设置（仅本机可访问）"
        return (
            "【B站值班室 · API】\n"
            f"HTTP：{base}/health · /status · /hot?count=5 · /video?bvid=BV… · /daily?count=3 · /notify\n"
            f"WebSocket：ws://{host}:{server.port}/ws\n"
            f"鉴权：{token}\n"
            "WebSocket 协议：发 {\"action\":\"daily\",\"params\":{\"count\":2},\"echo\":\"x\"}，收 "
            "{\"action\":\"daily\",\"status\":\"ok\",\"retcode\":0,\"data\":{…},\"echo\":\"x\"}"
        )

    # ---------------------------------------------------------------- commands
    @filter.command("bili", alias={"值班"})
    async def bili(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """B站值班室总入口。"""
        text = str(args or "").strip()
        sub, _, rest = text.partition(" ")
        sub = sub.strip() or "帮助"
        rest = rest.strip()

        if sub in ("帮助", "help", "?", ""):
            await self._send(event, HELP_TEXT)
            return

        if sub == "状态":
            await self._send(event, await self._status_text())
            return

        if sub in ("动态", "activity", "做了啥", "干了啥"):
            n = _int_param({"count": rest}, "count", 5, 1, 20) if rest else 5
            await self._send(event, self.activity_text(n))
            return

        if sub in ("api", "API", "端口"):
            await self._send(event, self._api_text())
            return

        if sub in ("presence", "PRESENCE", "常驻"):
            watcher = self._presence
            if watcher is None:
                await self._send(event, "常驻没启动（配置 presence_enable）。")
                return
            mode = "预览（不真发）" if watcher.dry_run else "实发"
            if rest in ("开", "打开"):
                watcher.dry_run = False
                await self._send(event, "好，切到实发模式：之后有人 @ 我/回我，会直接回一句。")
                return
            if rest in ("关", "关闭"):
                watcher.dry_run = True
                await self._send(event, "已切回预览模式，只记录不发送。")
                return
            if rest.startswith("预览"):
                count = _int_or(rest[2:], 3, 1, 10)
                items = await watcher.preview(count)
                if not items:
                    await self._send(event, "最近没有需要回的通知。")
                    return
                lines = [f"【常驻预览】最近 {len(items)} 条，我大概会这么回："]
                for entry in items:
                    brief = entry.get("item") or {}
                    lines.append(f"· {brief.get('user')}：{brief.get('content')}\n  → {entry.get('text')}")
                await self._send(event, "\n".join(lines))
                return
            if rest in ("立刻", "跑一轮"):
                done = await watcher.poll_once()
                await self._send(event, f"扫了一轮，处理 {len(done)} 条（{mode}）。")
                return
            state = watcher._state()
            await self._send(
                event,
                "【B站常驻】\n"
                f"状态：{'运行中' if watcher._running else '未运行'} · {mode}\n"
                f"轮询：每 {watcher.interval} 秒 · 上限 {watcher.max_per_hour} 条/小时\n"
                f"监听：{'、'.join(watcher.kinds)}\n"
                f"已记通知：{len(state.get('handled') or {})} 条 · 上次轮询：{watcher.last_poll or '还没跑'}\n"
                "指令：bili presence 预览 [n] / 开 / 关 / 立刻",
            )
            return

        if sub in ("订阅", "取消订阅"):
            if not self._store:
                return
            umo = event.unified_msg_origin
            if sub == "订阅":
                changed = self._store.subscribe(umo)
                await self._send(event, "已把本会话加入每日日报。" if changed else "本会话已经在名单里了。")
            else:
                changed = self._store.unsubscribe(umo)
                await self._send(event, "已把本会话移出每日日报。" if changed else "本会话本来就不在名单里。")
            return

        if sub == "热门":
            engine = await self._require_engine(event)
            if not engine:
                return
            count = _int_or(rest, 5, 1, 10)
            try:
                data = await run_bili(self._cli, "hot", "popular", "--size", str(max(count, 5)), timeout=60)
            except BiliError as exc:
                await self._send(event, f"拉热门失败：{exc}")
                return
            items = (data.get("items") or [])[:count]
            if not items:
                await self._send(event, "热门榜是空的。")
                return
            lines = [f"【B站热门】前 {len(items)} 条："]
            for index, item in enumerate(items, start=1):
                stat = item.get("stat") or {}
                lines.append(
                    f"{index}. {item.get('title')}\n"
                    f"   {item.get('owner') or '未知'} · {fmt_num(stat.get('view'))} 播放\n"
                    f"   {item.get('url') or item.get('bvid')}"
                )
            await self._send(event, "\n".join(lines))
            return

        if sub == "视频":
            engine = await self._require_engine(event)
            if not engine:
                return
            bvid = _extract_bvid(rest)
            if not bvid:
                await self._send(event, "用法：bili 视频 BV1xxxxxxxxx（或直接贴视频链接）")
                return
            try:
                video = await engine.digest(bvid)
            except BiliError as exc:
                await self._send(event, f"取 {bvid} 失败：{exc}")
                return
            stat = video.get("stat") or {}
            lines = [
                f"{video.get('title')}",
                f"UP：{video.get('owner')} · {fmt_num(stat.get('view'))} 播放 / {fmt_num(stat.get('like'))} 赞",
                f"{video.get('url')}",
            ]
            if video.get("chapters"):
                lines.append("章节：" + " · ".join(c.get("title", "") for c in video["chapters"][:6]))
            if video.get("ai_summary"):
                lines.append(f"AI 摘要：{video['ai_summary']}")
            transcript = (video.get("transcript") or "").strip()
            lines.append(f"字幕节选：{transcript[:400]}" if transcript else "这条没有字幕轨，只能看画面。")
            await self._send(event, "\n".join(lines))
            return

        if sub == "日报":
            engine = await self._require_engine(event)
            if not engine:
                return
            count = _int_or(rest, int(self._cfg().get("daily_count") or 3), 1, 6)
            await self._send(event, f"开始跑一轮（最多 {count} 条），大概 10–30 秒…")
            try:
                result = await engine.daily(count, self._data_dir)
            except BiliError as exc:
                await self._send(event, f"跑失败：{exc}")
                return
            await self._send(event, render_qq_message(result))
            return

        if sub == "评论":
            if not self._is_admin(event):
                await self._send(event, "只有管理员能发评论。")
                return
            if not bool(self._cfg().get("allow_comment", False)):
                await self._send(event, "评论开关是关的（配置 allow_comment）。")
                return
            bvid, _, content = rest.partition(" ")
            bvid = _extract_bvid(bvid)
            content = content.strip()
            if not bvid or not content:
                await self._send(event, "用法：bili 评论 BV1xxxxxxxxx 评论内容")
                return
            cmd = ["comment", "post", bvid, "--text", content]
            if bool(self._cfg().get("comment_dry_run", True)):
                cmd.append("--dry-run")
            try:
                data = await run_bili(self._cli, *cmd, timeout=60)
            except BiliError as exc:
                await self._send(event, f"评论失败：{exc}")
                return
            mode = "预演" if bool(self._cfg().get("comment_dry_run", True)) else "已发送"
            await self._send(event, f"评论{mode}：{data.get('message') or data}")
            return

        await self._send(event, f"不认识的子指令「{sub}」。\n\n{HELP_TEXT}")

    # ------------------------------------------------------------------ status
    async def _status_text(self) -> str:
        lines = ["【B站值班室 · 自检】"]
        if not self._cli:
            lines.append("引擎：✗ 没找到 bili（配置 cli_path）")
            return "\n".join(lines)
        lines.append(f"引擎：{self._cli}")
        lines.append(f"版本：{await version(self._cli)}")
        if self._engine:
            try:
                auth = await self._engine.auth_status()
                user = auth.get("user") or {}
                if auth.get("logged_in"):
                    lines.append(f"账号：✓ {user.get('name') or user.get('uname') or ''} (UID {user.get('mid')})")
                else:
                    lines.append("账号：✗ 未登录（在服务器上跑 `bili auth login`）")
            except BiliError as exc:
                lines.append(f"账号：✗ {exc}")
        lines.append(f"定时：{self._cfg().get('daily_time') or '21:30'} · {'开' if self._cfg().get('daily_enable', True) else '关'}")
        if self._api is not None:
            lines.append(f"API：http://{self._api.host}:{self._api.port} · {len(self._api.handlers)} 个 action")
        else:
            lines.append("API：未启动（bili api 查看原因）")
        if self._presence is not None:
            mode = "预览" if self._presence.dry_run else "实发"
            lines.append(
                f"常驻：{'运行中' if self._presence._running else '待启动'} · {mode} · "
                f"每 {self._presence.interval}s 看一次 @/回复"
            )
        else:
            lines.append("常驻：未启用（配置 presence_enable）")
        if self._store:
            lines.append(f"日报会话：{len(self._store.sessions)} 个")
            last = self._store.last_run()
            lines.append(f"上次运行：{last or '还没跑过'}")
        if self._journal is not None:
            cfg = self._cfg()
            auto = "开" if cfg.get("auto_comment_enable", False) else "关"
            dry = "（预演）" if cfg.get("auto_comment_dry_run", True) else "（实发）"
            lines.append(
                f"主动评论：{auto}{dry} · 今天已评 {self._journal.count_today('comment')} 条 · "
                f"上限 {cfg.get('auto_comment_daily') or 2}/天"
            )
            lines.append(
                f"活动日志：{len(self._journal.entries)} 条（bili 动态 查看）"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------ daily runner
    async def _daily_loop(self) -> None:
        while True:
            try:
                await self._maybe_run_daily()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[bili-duty] daily loop error")
            await asyncio.sleep(30)

    async def _maybe_run_daily(self) -> None:
        if not (self._engine and self._store and self._store.sessions):
            return
        target = str(self._cfg().get("daily_time") or "21:30").strip()
        try:
            hour, minute = (int(x) for x in target.split(":", 1))
        except ValueError:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self._last_day == today:
            return
        if (now.hour, now.minute) < (hour, minute):
            return
        self._last_day = today
        await self._run_daily_for_sessions()

    async def _run_daily_for_sessions(self) -> None:
        if not (self._engine and self._store):
            return
        if self._running:
            return
        self._running = True
        try:
            count = int(self._cfg().get("daily_count") or 3)
            result = await self._engine.daily(count, self._data_dir)
            text = render_qq_message(result)
            for umo in list(self._store.sessions):
                try:
                    await self.context.send_message(umo, MessageChain([Plain(text)]))
                except Exception:
                    logger.exception(f"[bili-duty] 推送 {umo} 失败")
            logger.info(f"[bili-duty] daily done: {result.get('path')}")
        except BiliError as exc:
            logger.error(f"[bili-duty] daily failed: {exc}")
            if bool(self._cfg().get("notify_error", True)):
                for umo in list(self._store.sessions):
                    try:
                        await self.context.send_message(umo, MessageChain([Plain(f"【B站值班】今天跑失败了：{exc}")]))
                    except Exception:
                        pass
        finally:
            self._running = False


def _up_name(item: dict[str, Any]) -> str:
    for key in ("author", "owner", "up"):
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("uname")
        if value:
            return str(value)
    return str(item.get("uname") or item.get("author_name") or "未知")


def _fmt_dur(value: Any) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return str(value or "未知")
    if seconds <= 0:
        return "未知"
    minutes, rest = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {rest} 秒"
    return f"{rest} 秒"


def _split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _int_param(params: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value: Any = params.get(key, default)
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _now_ts() -> float:
    return datetime.now().timestamp()


def _int_or(text: str, default: int, low: int, high: int) -> int:
    for token in str(text or "").split():
        if token.isdigit():
            return max(low, min(high, int(token)))
    return default


def _extract_bvid(text: str) -> str | None:
    import re

    match = re.search(r"(BV[0-9A-Za-z]{10})", str(text or ""))
    return match.group(1) if match else None
