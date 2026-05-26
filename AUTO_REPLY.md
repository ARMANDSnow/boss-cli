# boss-cli Auto-Reply + MCP 集成

针对招聘 HR 场景：候选人主动打招呼后，让 agent（Codex / Claude Code / 任意 MCP 宿主）按模板池自动回一句"麻烦发份简历"。

风控边界：只回**还没回过**的新招呼，工作时段才跑，自带每日上限 + 发送间隔 + 打字延迟 + 候选人首句二次核对。

---

## 一、安装

```bash
# 需要 Python ≥ 3.10
uv tool install --from . 'kabi-boss-cli[mcp,yaml]'   # 从本仓库源码装
# 或: uv tool install 'kabi-boss-cli[mcp,yaml]'      # 从 PyPI 装 (上游版本，不含本 fork 改动)

# 首次登录（从浏览器导 cookie 或扫码）
boss login
```

入口：

| 命令 | 作用 |
|---|---|
| `boss recruiter pending` | 列出"等我回复"的候选人（不发） |
| `boss recruiter auto-reply --dry-run` | 预览将发送的内容 |
| `boss recruiter auto-reply -y` | 真发 |
| `boss recruiter templates [--add ...] [--reset]` | 管理模板池 |
| `boss-cli-mcp` | 启动 MCP server（stdio） |

---

## 二、模板池

```bash
cp templates.txt.example ~/.config/boss-cli/templates.txt
$EDITOR ~/.config/boss-cli/templates.txt
```

格式：一行一条；`#` 开头注释；`{name}` 占位符替换为候选人姓名。
建议 **≥ 8 条**，避免被 NLP 指纹检测。

---

## 三、Codex CLI 集成（MCP）

在 `~/.codex/config.toml` 加：

```toml
[mcp_servers.boss]
command = "boss-cli-mcp"
# 如果 boss-cli-mcp 不在 PATH，写绝对路径，例如：
# command = "/Users/dingyuxuan/.local/bin/boss-cli-mcp"
```

确认：

```bash
codex mcp list      # 应看到 boss
```

之后在 Codex 里直接说：

> 帮我看下 boss 有谁等我回复，先 dry_run 看下要发什么，然后帮我真的发出去。

Codex 会顺序调：`boss.list_pending` → `boss.auto_reply(dry_run=true)` → 让你确认 → `boss.auto_reply(dry_run=false)`。

### Claude Code 集成

```bash
claude mcp add boss boss-cli-mcp
```

### 任意 MCP 宿主

stdio transport，命令 `boss-cli-mcp`，无需 env。

---

## 四、MCP tools

| Tool | 入参 | 出参要点 |
|---|---|---|
| `list_pending` | `enc_job_id?`, `strict_check=true` | `candidates[{friend_id,name,job_name,last_text,last_time}]` |
| `auto_reply` | `dry_run=true`, `daily_limit=80`, `max_send?`, `ignore_hours=false`, `strict_check=true`, `enc_job_id?` | `sent[]`, `skipped[]`, `quota_remaining` |
| `list_templates` | — | `templates[]`, `path` |
| `add_template` | `template` | `count` |
| `reset_templates` | — | `templates[]` |

**Agent 推荐流程**：先 `list_pending` 给用户看 → 用户同意后 `auto_reply(dry_run=true)` 给计划 → 再 `auto_reply(dry_run=false)` 真发。

---

## 五、内置风控护栏（hard-coded）

| 维度 | 默认值 | 改在哪 |
|---|---|---|
| 工作时段 | 09:30-12:00 / 14:00-19:00 | `boss_cli/auto_reply.py: WORK_HOURS` |
| 发送间隔 | 12-30s 随机 | `SEND_DELAY_MIN/MAX` |
| 打字延迟 | `len(msg) / 6 chars/sec + 0-1.5s 抖动` | `TYPING_CHARS_PER_SEC` |
| 日发送上限 | 80 条 | `DEFAULT_DAILY_QUOTA` 或 `--limit` |
| 候选人二次核对 | 拉聊天历史确认无 recruiter 回复 | `--no-strict` 关闭 |
| 错误熔断 | 出现 `code 9` / `stoken` 错误立刻 break 本批 | `run_auto_reply` 内 |
| 审计日志 | `~/.config/boss-cli/auto_reply_audit.jsonl` | 每发一条记一行 |
| 配额持久化 | `~/.config/boss-cli/auto_reply_quota.json` | 按日期分桶 |

---

## 六、运维建议

1. **本地家庭宽带跑**，别上 VPS（Boss 对机房 IP 段重点打标）。
2. **同账号同设备指纹**：跑 agent 的机器必须是你平时在 Chrome 登录 Boss 的那台。
3. **第一周保守跑**：先 `--max-send 5`，观察候选人回复率和是否触发任何验证码。
4. **定期 review 审计日志**：`tail ~/.config/boss-cli/auto_reply_audit.jsonl`。
5. **测试号先行**：别第一次就接公司主账号。

---

## 七、已知限制

- 上游 issue [#21/#16/#4](https://github.com/jackwener/boss-cli/issues) — 「主动 greet」需要 `__zp_stoken__`，本工具不支持，**只能回已经主动联系你的候选人**。这正好符合你的场景。
- Boss 用户协议禁止自动化脚本，账号风险永远存在。**关键决策（约面试、要电话）保留人工操作**。

---

## 八、本 fork 相对上游的改动

- 新增 `boss_cli/auto_reply.py` — 模板池/配额/工作时段/审计逻辑
- 新增 `boss_cli/mcp_server.py` — MCP server (stdio)
- 在 `boss_cli/commands/recruiter.py` 加 3 个 Click 子命令：`pending` / `auto-reply` / `templates`
- `pyproject.toml`：加 `[mcp]` 可选依赖、加 `boss-cli-mcp` entry point
