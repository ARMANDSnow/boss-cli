"""MCP server exposing boss-cli auto-reply features to Codex / Claude / any MCP host.

Tools exposed:
- list_pending      — show candidates waiting for first recruiter reply
- auto_reply        — send a templated reply to each pending candidate
                      (defaults to dry_run=True; agent must pass dry_run=False to actually send)
- pacing_status     — inspect current pulse state + intraday intensity
- list_templates    — read current template pool
- add_template      — append a template to the pool
- reset_templates   — restore the built-in template pool

Run with `boss-cli-mcp` (entry point) or `python -m boss_cli.mcp_server`.
Uses stdio transport, the default for Codex CLI / Claude Code MCP hosts.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .auth import get_credential
from .auto_reply import (
    DEFAULT_DAILY_QUOTA,
    DEFAULT_TEMPLATES,
    is_within_work_hours,
    list_pending,
    load_templates,
    pacing_snapshot,
    run_auto_reply,
    save_templates,
)
from .client import BossClient

logger = logging.getLogger("boss_cli.mcp")


def _require_credential():
    cred = get_credential()
    if not cred:
        raise RuntimeError("未登录。请先在终端运行 `boss login` 完成扫码登录。")
    return cred


def _list_pending_impl(enc_job_id: str = "", strict_check: bool = True) -> dict:
    cred = _require_credential()
    with BossClient(cred) as c:
        items = list_pending(c, enc_job_id=enc_job_id, strict_check=strict_check)
    return {
        "count": len(items),
        "within_work_hours": is_within_work_hours(),
        "candidates": [
            {
                "friend_id": p.friend_id, "uid": p.uid, "name": p.name,
                "job_name": p.job_name, "last_text": p.last_text, "last_time": p.last_time,
            }
            for p in items
        ],
    }


def _auto_reply_impl(
    enc_job_id: str = "",
    dry_run: bool = True,
    ignore_hours: bool = False,
    daily_limit: int = DEFAULT_DAILY_QUOTA,
    max_send: int | None = None,
    strict_check: bool = True,
    respect_pacing: bool = True,
) -> dict:
    cred = _require_credential()
    templates = load_templates()
    with BossClient(cred) as c:
        candidates = list_pending(c, enc_job_id=enc_job_id, strict_check=strict_check)
        if not candidates:
            return {"sent_count": 0, "skipped_count": 0, "quota_remaining": None,
                    "dry_run": dry_run, "sent": [], "skipped": [],
                    "note": "no pending candidates"}
        report = run_auto_reply(
            c, candidates, templates,
            daily_limit=daily_limit, dry_run=dry_run,
            ignore_hours=ignore_hours, max_send=max_send,
            respect_pacing=respect_pacing, enc_job_id=enc_job_id,
        )
    return report.summary()


def _pacing_status_impl() -> dict:
    return pacing_snapshot()


def _list_templates_impl() -> dict:
    return {"templates": load_templates(), "path": str(__import__("boss_cli.auto_reply", fromlist=["TEMPLATES_PATH"]).TEMPLATES_PATH)}


def _add_template_impl(template: str) -> dict:
    templates = load_templates()
    templates.append(template)
    save_templates(templates)
    return {"count": len(templates), "added": template}


def _reset_templates_impl() -> dict:
    save_templates(list(DEFAULT_TEMPLATES))
    return {"count": len(DEFAULT_TEMPLATES), "templates": list(DEFAULT_TEMPLATES)}


# ── MCP server wiring ───────────────────────────────────────────────


def _build_server():
    try:
        from mcp.server import Server  # type: ignore[import-not-found]
        from mcp.server.stdio import stdio_server  # type: ignore[import-not-found]
        from mcp.types import TextContent, Tool  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "需要 mcp 依赖。请用: `uv tool install kabi-boss-cli[mcp]` "
            "或 `pip install 'mcp>=1.0'` 后重试。"
        ) from exc

    server = Server("boss-cli")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="list_pending",
                description="列出 Boss 直聘上「等我回复」的候选人 (label=新招呼 且 我尚未回过)。返回 friend_id / 姓名 / 岗位 / 候选人首句。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "enc_job_id": {"type": "string", "description": "可选: 按职位 encryptJobId 过滤"},
                        "strict_check": {"type": "boolean", "default": True,
                                         "description": "是否拉聊天历史二次确认 '真的没回过' (慢但准)"},
                    },
                },
            ),
            Tool(
                name="auto_reply",
                description=(
                    "对 pending 候选人按模板池随机回一句。"
                    "默认 dry_run=True (只返回计划不真发); 默认 respect_pacing=True (单次调用最多发 1 条, "
                    "由 pulse 节奏决定; 适合 agent 每 10 分钟周期触发)。"
                    "已内置: 看人再回 (view+history+8-25s 阅读停顿)、burst+rest pulse 节奏 (3-5 条/簇, 15-40min 休息)、"
                    "时段强度曲线 (09:30-19:00, 午休 12:00-13:30 硬静默)、每日 80 条上限、打字延迟、code=9 熔断。"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "enc_job_id": {"type": "string", "description": "可选: 按职位 encryptJobId 过滤"},
                        "dry_run": {"type": "boolean", "default": True,
                                    "description": "true=只规划不真发 (默认), false=真实发送"},
                        "respect_pacing": {"type": "boolean", "default": True,
                                           "description": "true (默认) = 单次调用至多发 1 条, 由 pulse 节奏决定;"
                                                          " false = 批量发, 内部 sleep 数小时直到全部发完"},
                        "ignore_hours": {"type": "boolean", "default": False,
                                         "description": "跳过工作时段闸门 (不推荐)"},
                        "daily_limit": {"type": "integer", "default": DEFAULT_DAILY_QUOTA,
                                        "description": f"今日发送上限 (默认 {DEFAULT_DAILY_QUOTA})"},
                        "max_send": {"type": "integer",
                                     "description": "本次最多发送 N 条 (省略=不限)"},
                        "strict_check": {"type": "boolean", "default": True},
                    },
                },
            ),
            Tool(
                name="pacing_status",
                description=(
                    "查看当前 pulse 节奏状态: 工作强度 (0=静默, 1=峰值)、当前 burst 进度、距下次可发还有几秒、"
                    "下一步动作 (send/wait/rest/silent) 及原因。agent 用 auto_reply 被 skip 时调这个看为什么。"
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="list_templates",
                description="读当前自动回复模板池 (~/.config/boss-cli/templates.txt)",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="add_template",
                description="向模板池追加一条新模板",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "template": {"type": "string", "description": "模板内容, 可含 {name} 占位符"},
                    },
                    "required": ["template"],
                },
            ),
            Tool(
                name="reset_templates",
                description="重置为内置模板池 (5 条通用 '请发简历' 措辞)",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list:
        import json as _json
        try:
            if name == "list_pending":
                result = _list_pending_impl(**arguments)
            elif name == "auto_reply":
                result = _auto_reply_impl(**arguments)
            elif name == "pacing_status":
                result = _pacing_status_impl()
            elif name == "list_templates":
                result = _list_templates_impl()
            elif name == "add_template":
                result = _add_template_impl(**arguments)
            elif name == "reset_templates":
                result = _reset_templates_impl()
            else:
                result = {"error": f"unknown tool: {name}"}
        except Exception as exc:  # noqa: BLE001
            result = {"error": str(exc), "tool": name}
            logger.exception("tool %s failed", name)
        return [TextContent(type="text", text=_json.dumps(result, ensure_ascii=False, indent=2))]

    return server, stdio_server


async def _async_main() -> None:
    server, stdio_server = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="boss-cli-mcp %(levelname)s %(message)s")
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
