"""B站常驻：盯着「@我的」和「回复我的」，被叫到就回一句。

设计取舍（和 scroll 的红线一致）：

* 只做**被动回应**——别人 @ 我、回复我，我才回；不主动刷评论、不关注陌生人、
  不群发私信。
* 所有写动作都经由 ``bili`` CLI：它自带 guard（comment 10 秒间隔 / 30 条每小时）
  和 audit.log，插件不自己发请求。
* 第一次启动只做基线：把当时看到的通知 id 记下，不回；之后新出现的才回，避免
  上线瞬间把历史积压全回一遍。
* 默认 ``dry_run``：只把「本来要发什么」写进 ``presence.log``，不真发。
* 去重、频率上限都落在 ``state.json`` 里，重启不会重复回、不会突破小时上限。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from .bili_engine import BiliError, run_bili

ReplyFn = Callable[[str, dict[str, Any]], Awaitable[str]]

DEFAULT_SYSTEM_PROMPT = (
    "你在 B站 评论区回复别人。用中文，一句到两句，30 字以内，口气轻松自然，"
    "不要客服腔，不要自我介绍，不要提自己是 AI 或机器人，不要刷表情。"
)


class PresenceWatcher:
    """Poll Bilibili notifications and answer the ones addressed to us."""

    def __init__(
        self,
        cli: str,
        store: Any,
        *,
        interval: int = 90,
        max_per_hour: int = 6,
        max_age_hours: int = 12,
        mode: str = "llm",
        template: str = "收到～",
        dry_run: bool = True,
        kinds: tuple[str, ...] = ("mentions", "replies"),
        reply_fn: ReplyFn | None = None,
        logger: Any = None,
        log_path: Path | None = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
        on_sent: Callable[[dict[str, Any], str], Awaitable[None]] | None = None,
    ) -> None:
        self.cli = cli
        self.store = store
        self.interval = max(30, int(interval))
        self.max_per_hour = max(1, int(max_per_hour))
        self.max_age = timedelta(hours=max(1, int(max_age_hours)))
        self.mode = (mode or "llm").lower()
        self.template = template or "收到～"
        self.dry_run = bool(dry_run)
        self.kinds = tuple(k for k in kinds if k in ("mentions", "replies")) or ("mentions",)
        self.reply_fn = reply_fn
        self.logger = logger
        self.log_path = log_path
        self.notify = notify
        self.on_sent = on_sent
        self._running = False
        self.last_poll: str | None = None
        self.last_error: str | None = None
        self._stop = asyncio.Event()
        # a runtime toggle (bili presence 开/关) wins over the config default
        override = self._state().get("dry_run")
        if isinstance(override, bool):
            self.dry_run = override

    def set_dry_run(self, value: bool) -> None:
        """Persist the 开/关 toggle so a plugin reload keeps the same mode."""
        self.dry_run = bool(value)
        self._state()["dry_run"] = self.dry_run
        self._save()

    # ------------------------------------------------------------------ state
    def _state(self) -> dict[str, Any]:
        return self.store.data.setdefault(
            "presence", {"handled": {}, "sent": [], "bootstrapped": False}
        )

    def _save(self) -> None:
        try:
            self.store.save()
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning(f"[presence] state save failed: {exc}")

    def _hourly_count(self, state: dict[str, Any]) -> int:
        cutoff = time.time() - 3600
        sent = [float(x) for x in state.get("sent") or [] if float(x) > cutoff]
        state["sent"] = sent
        return len(sent)

    def _prune(self, state: dict[str, Any]) -> None:
        handled = state.get("handled") or {}
        cutoff = time.time() - 30 * 86400
        state["handled"] = {
            str(key): value for key, value in handled.items() if float(value) > cutoff
        }

    # -------------------------------------------------------------------- loop
    async def run(self) -> None:
        self._running = True
        if self.logger:
            state = "预览（dry-run，不真发）" if self.dry_run else "实发"
            self.logger.info(
                f"[presence] watcher started · 每 {self.interval}s 轮询 {','.join(self.kinds)} · "
                f"上限 {self.max_per_hour}/小时 · {state}"
            )
        while not self._stop.is_set():
            try:
                await self.poll_once()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self.logger:
                    self.logger.warning(f"[presence] poll failed: {self.last_error}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- fetch
    async def fetch(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for kind in self.kinds:
            try:
                data = await run_bili(self.cli, "feed", kind, "--limit", "20", timeout=60)
            except BiliError as exc:
                if self.logger:
                    self.logger.debug(f"[presence] feed {kind} failed: {exc}")
                continue
            for item in data.get("items") or []:
                if isinstance(item, dict):
                    item = dict(item)
                    item["_kind"] = kind
                    items.append(item)
        items.sort(key=lambda x: str(x.get("time") or ""))
        return items

    # ------------------------------------------------------------------- reply
    async def compose(self, item: dict[str, Any]) -> str:
        content = str(item.get("content") or "").strip()
        if not content:
            return ""
        if self.mode == "llm" and self.reply_fn is not None:
            prompt = self._prompt(item)
            try:
                text = await self.reply_fn(prompt, item)
                if text and text.strip():
                    return _clean(text)[:140]
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.debug(f"[presence] llm reply failed: {exc}")
        user = (item.get("user") or {}).get("name") or "你"
        try:
            return _clean(self.template.format(name=user))[:140]
        except (KeyError, IndexError):
            return _clean(self.template)[:140]

    def _prompt(self, item: dict[str, Any]) -> str:
        user = (item.get("user") or {}).get("name") or "某人"
        on = item.get("on") or {}
        title = on.get("title") or ""
        where = f"在《{title}》下" if title else "在动态下"
        kind = "回复你" if item.get("_kind") == "replies" else "@你"
        quoted = item.get("replying_to")
        extra = f"\n（对方这条是在回你之前说的：{quoted}）" if quoted else ""
        return (
            f"{user} {where}{kind}说：{item.get('content')}{extra}\n"
            "回他一句，30 字以内。"
        )

    async def post(self, item: dict[str, Any], text: str) -> tuple[bool, str]:
        on = item.get("on") or {}
        target = on.get("bvid") or on.get("oid")
        rpid = item.get("rpid")
        if not target or not rpid:
            return False, "缺少 target 或 rpid"
        args: list[str] = ["comment", "reply", str(target), str(rpid)]
        root = item.get("root")
        if root:
            args += ["--root", str(root)]
        kind = str(on.get("type") or "video")
        if kind not in ("video", ""):
            args += ["--type", kind]
        args.append(text)
        command = "bili " + " ".join(_quote(a) for a in args)
        if self.dry_run:
            self._log({"ts": _now_iso(), "mode": "dry-run", "command": command, "item": _brief(item), "text": text})
            return True, "dry-run"
        try:
            data = await run_bili(self.cli, *args, timeout=60)
        except BiliError as exc:
            self._log({"ts": _now_iso(), "mode": "error", "command": command, "error": str(exc), "item": _brief(item)})
            return False, str(exc)
        self._log({"ts": _now_iso(), "mode": "sent", "command": command, "result": data, "item": _brief(item), "text": text})
        return True, "sent"

    def _log(self, record: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------------- poll
    async def poll_once(self) -> list[dict[str, Any]]:
        items = await self.fetch()
        self.last_poll = _now_iso()
        state = self._state()

        if not state.get("bootstrapped"):
            for item in items:
                state["handled"][str(item.get("id"))] = time.time()
            state["bootstrapped"] = True
            self._save()
            if self.logger:
                self.logger.info(f"[presence] baseline {len(items)} 条通知已记下（不回历史积压）")
            return []

        handled: dict[str, float] = state.setdefault("handled", {})
        done: list[dict[str, Any]] = []
        now = datetime.now().astimezone()

        for item in items:
            key = str(item.get("id"))
            if key in handled:
                continue
            when = _parse_time(item.get("time"))
            if when is None or (now - when) > self.max_age:
                handled[key] = time.time()
                continue
            if self._hourly_count(state) >= self.max_per_hour:
                if self.logger:
                    self.logger.info("[presence] 本小时额度用完，剩下的下轮再说")
                break
            text = await self.compose(item)
            if not text:
                handled[key] = time.time()
                continue
            ok, detail = await self.post(item, text)
            handled[key] = time.time()
            if ok:
                if detail == "sent":
                    state["sent"].append(time.time())
                done.append({"item": _brief(item), "text": text, "mode": detail})
                if self.logger:
                    self.logger.info(f"[presence] {detail}: {text}  ←  {_brief(item).get('user')}")
                if self.notify is not None and detail == "sent":
                    try:
                        await self.notify(f"B站回复了一条：{text}\n来源：{_brief(item).get('url')}")
                    except Exception:  # noqa: BLE001
                        pass
                if self.on_sent is not None and detail == "sent":
                    try:
                        await self.on_sent(item, text)
                    except Exception as exc:  # noqa: BLE001
                        if self.logger:
                            self.logger.debug(f"[presence] on_sent hook failed: {exc}")
            else:
                if self.logger:
                    self.logger.warning(f"[presence] 回复失败：{detail}")

        self._prune(state)
        self._save()
        return done

    # ----------------------------------------------------------------- preview
    async def preview(self, count: int = 3) -> list[dict[str, Any]]:
        items = [i for i in await self.fetch() if str(i.get("content") or "").strip()]
        picked = items[-max(1, count):]
        out: list[dict[str, Any]] = []
        for item in picked:
            text = await self.compose(item)
            out.append({"item": _brief(item), "text": text})
        return out


def _clean(text: str) -> str:
    return " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _quote(arg: str) -> str:
    text = str(arg)
    return f'"{text}"' if (" " in text or not text) else text


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    on = item.get("on") or {}
    user = item.get("user") or {}
    content = _clean(item.get("content") or "")
    return {
        "id": item.get("id"),
        "kind": item.get("_kind"),
        "user": user.get("name"),
        "mid": user.get("mid"),
        "time": item.get("time"),
        "content": content[:120],
        "title": on.get("title"),
        "url": on.get("url"),
    }
