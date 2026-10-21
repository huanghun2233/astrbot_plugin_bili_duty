"""Duty logic: pick what is worth watching, digest it, render a report.

This is the part that used to be a 15.9 KB charter prompt in dsh-bili-scroll.
Here it is deterministic code: collect -> dedupe -> pick -> digest -> render.
The judgement text stays with the operator (or an LLM) -- the plugin only
supplies the material and the guard rails.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # 作为 AstrBot 插件包导入
    from .bili_engine import BiliError, run_bili
except ImportError:  # 直接跑单文件调试
    from bili_engine import BiliError, run_bili

SOURCES = ("recommend", "hot", "search")


def _duration_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    if ":" in text:
        try:
            parts = [int(p) for p in text.split(":")]
        except ValueError:
            return 0
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def fmt_num(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value or "0")
    if number >= 10000:
        return f"{number / 10000:.1f} 万"
    return str(number)


class StateStore:
    """Small JSON state: seen BVIDs, daily targets, last run."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "state.json"
        self.data = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # --- seen -----------------------------------------------------------
    @property
    def seen(self) -> dict[str, str]:
        return self.data.setdefault("seen", {})

    def mark_seen(self, bvid: str, day: str) -> None:
        self.seen[bvid] = day

    def prune_seen(self, keep_days: int = 60) -> None:
        today = datetime.now()
        kept = {}
        for bvid, day in self.seen.items():
            try:
                age = (today - datetime.strptime(day, "%Y-%m-%d")).days
            except Exception:
                age = 0
            if age <= keep_days:
                kept[bvid] = day
        self.data["seen"] = kept

    # --- daily targets --------------------------------------------------
    @property
    def sessions(self) -> list[str]:
        return self.data.setdefault("sessions", [])

    def subscribe(self, umo: str) -> bool:
        if umo in self.sessions:
            return False
        self.sessions.append(umo)
        self.save()
        return True

    def unsubscribe(self, umo: str) -> bool:
        if umo not in self.sessions:
            return False
        self.sessions.remove(umo)
        self.save()
        return True

    # --- run bookkeeping ------------------------------------------------
    def last_run(self) -> str | None:
        return self.data.get("last_run")


class DutyEngine:
    """Collect candidates, digest them, render a scroll-style report."""

    def __init__(
        self,
        cli: str,
        *,
        min_duration: int = 120,
        max_duration: int = 2400,
        max_chars: int = 4000,
        keywords: list[str] | None = None,
        logger=None,
    ) -> None:
        self.cli = cli
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.max_chars = max_chars
        self.keywords = keywords or []
        self.log = logger

    def _debug(self, message: str) -> None:
        if self.log:
            try:
                self.log.info(message)
            except Exception:
                pass

    async def auth_status(self) -> dict:
        return await run_bili(self.cli, "auth", "status", timeout=30)

    async def collect(self, sources: tuple[str, ...] = ("recommend", "hot")) -> list[dict]:
        items: list[dict] = []
        if "recommend" in sources:
            try:
                data = await run_bili(self.cli, "feed", "recommend", "-n", "12", "--unseen", timeout=60)
                items.extend(data.get("items") or [])
            except BiliError as exc:
                self._debug(f"[bili-duty] recommend failed: {exc}")
        if "hot" in sources:
            try:
                data = await run_bili(self.cli, "hot", "popular", "--size", "20", "--unseen", timeout=60)
                items.extend(data.get("items") or [])
            except BiliError as exc:
                self._debug(f"[bili-duty] hot failed: {exc}")
        if "search" in sources:
            for keyword in self.keywords:
                try:
                    data = await run_bili(self.cli, "search", "video", keyword, "--limit", "5", timeout=60)
                    items.extend(data.get("items") or [])
                except BiliError as exc:
                    self._debug(f"[bili-duty] search {keyword} failed: {exc}")
        return items

    def pick(self, items: list[dict], seen: dict[str, str], count: int) -> list[dict]:
        picked: list[dict] = []
        for item in items:
            bvid = item.get("bvid")
            if not bvid or bvid in seen or any(p.get("bvid") == bvid for p in picked):
                continue
            duration = _duration_seconds(item.get("duration"))
            if duration and not (self.min_duration <= duration <= self.max_duration):
                continue
            item["duration"] = duration
            picked.append(item)
            if len(picked) >= count:
                break
        return picked

    async def digest(self, bvid: str) -> dict:
        data = await run_bili(
            self.cli,
            "video", "watch", bvid,
            "--max-chars", str(self.max_chars),
            "--comments", "3",
            "--frames", "9",
            "--transcript", "auto",
            timeout=180,
        )
        transcript = data.get("transcript") or {}
        return {
            "bvid": bvid,
            "title": data.get("title"),
            "owner": (data.get("owner") or {}).get("name"),
            "duration": data.get("duration"),
            "url": data.get("url"),
            "stat": data.get("stat") or {},
            "tags": data.get("tags") or [],
            "chapters": data.get("chapters") or [],
            "ai_summary": data.get("ai_summary"),
            "transcript_source": transcript.get("source"),
            "transcript": (transcript.get("text") or "")[: self.max_chars],
        }

    async def daily(self, count: int, data_dir: Path) -> dict:
        """One full sweep. Returns {'date', 'videos', 'path'}."""
        day = datetime.now().strftime("%Y-%m-%d")
        items = await self.collect(("recommend", "hot", "search"))
        store = StateStore(data_dir)
        picked = self.pick(items, store.seen, count)
        self._debug(f"[bili-duty] candidates={len(items)} picked={len(picked)}")

        videos = []
        for item in picked:
            try:
                videos.append(await self.digest(item["bvid"]))
            except BiliError as exc:
                self._debug(f"[bili-duty] digest {item['bvid']} failed: {exc}")

        for video in videos:
            store.mark_seen(video["bvid"], day)
        store.data["last_run"] = datetime.now().isoformat(timespec="seconds")
        store.prune_seen()
        store.save()

        report = render_report(day, videos)
        out_dir = data_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{day}.md"
        path.write_text(report, encoding="utf-8")
        return {"date": day, "videos": videos, "path": str(path), "report": report}


def render_report(day: str, videos: list[dict]) -> str:
    """Markdown in the shape dsh-bili-scroll uses: one ``### BV号`` per pick."""
    lines = [f"# B站值班日报 · {day}", ""]
    if not videos:
        lines.append("今天没有挑到新片子（都看过了）。")
        return "\n".join(lines)
    for video in videos:
        stat = video.get("stat") or {}
        head = (
            f"### {video.get('bvid')} — {video.get('title') or ''}\n"
            f"- UP：{video.get('owner') or '未知'} · 时长：{_duration_seconds(video.get('duration')) // 60} 分钟"
            f" · {fmt_num(stat.get('view'))} 播放 / {fmt_num(stat.get('like'))} 赞 / {fmt_num(stat.get('reply'))} 评论\n"
            f"- 链接：{video.get('url') or ''}"
        )
        chapters = video.get("chapters") or []
        if chapters:
            head += "\n- 章节：" + " · ".join(
                f"{c.get('title')}" for c in chapters[:6] if c.get("title")
            )
        lines.append(head)
        if video.get("ai_summary"):
            lines.append(f"- AI 摘要：{video['ai_summary']}")
        text = (video.get("transcript") or "").strip()
        if text:
            lines.append(f"- 字幕节选：{text[:600]}")
        else:
            lines.append("- 字幕节选：（这条没有字幕轨，只能看画面）")
        lines.append("")
    return "\n".join(lines)


def render_qq_message(result: dict, limit: int = 3) -> str:
    """Compact plain-text version for a QQ message (no markdown tables)."""
    videos = result.get("videos") or []
    if not videos:
        return f"【B站值班】{result.get('date')} 没有新片子了，今天先这样。"
    parts = [f"【B站值班】{result.get('date')} 今天挑了 {len(videos)} 条："]
    for index, video in enumerate(videos[:limit], start=1):
        stat = video.get("stat") or {}
        parts.append(
            f"{index}. {video.get('title')}\n"
            f"   {video.get('owner')} · {_duration_seconds(video.get('duration')) // 60} 分钟 · "
            f"{fmt_num(stat.get('view'))} 播放\n"
            f"   {video.get('url')}"
        )
    parts.append("完整日报已经写进插件的 out 目录。")
    return "\n".join(parts)
