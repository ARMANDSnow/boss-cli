# Boss 自动回复 · 小白上手指南

让 AI 助手（Codex / Claude / Workbuddy 等桌面端）替你自动回复 Boss 直聘候选人的第一条消息，比如"好的，麻烦发份简历"。

---

## 这个工具能做什么 / 不能做什么

**✅ 能做**
- 列出"等你回复"的候选人（候选人主动打招呼且你还没回过的）
- 按你预设的模板池随机回一句
- 在 AI agent 里用大白话调用："看下有谁等我回复，帮我都回了"

**❌ 不能做**
- 主动给陌生候选人打招呼（Boss 风控屏蔽，不要做）
- 替你做面试决策、要电话微信（这些要你亲自来）

**⚠️ 你必须知道**
- Boss 用户协议禁止自动化脚本，账号有被风控的风险
- 第一次用建议拿小号试，不要直接上公司主账号
- 默认只在工作时间（9:30-12:00 / 14:00-19:00）跑，每天最多 80 条

---

## 第 0 步：准备环境（一次性，10 分钟）

你需要 Mac 或 Linux。Windows 也能跑但步骤略不同，本文以 Mac 为例。

打开 **终端**（Terminal app），逐条复制粘贴：

```bash
# 1. 装 Homebrew（如果你已经有了，跳过）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. 装 uv（Python 包管理器）
brew install uv

# 3. 装 gh（GitHub 命令行，用来下载代码）
brew install gh

# 4. 登录 GitHub
gh auth login
# 按提示选 GitHub.com → HTTPS → Login with a web browser，把屏幕上的 8 位码贴到浏览器
```

验证：

```bash
uv --version    # 应该显示 uv 0.x.x
gh auth status  # 应该显示 ✓ Logged in
```

---

## 第 1 步：下载并安装工具（3 分钟）

```bash
# 下载到 ~/dev 目录（没有的话会自动建）
mkdir -p ~/dev
cd ~/dev
gh repo clone ARMANDSnow/boss-cli
cd boss-cli

# 创建虚拟环境并安装
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[mcp,yaml]"
```

验证：

```bash
boss --version           # 应该显示 boss, version 0.x.x
which boss-cli-mcp       # 应该显示 .../boss-cli/.venv/bin/boss-cli-mcp
```

把这一行的输出**记下来**，等会儿配 Codex 要用：

```bash
echo "$(pwd)/.venv/bin/boss-cli-mcp"
# 例如输出：/Users/dingyuxuan/dev/boss-cli/.venv/bin/boss-cli-mcp
```

---

## 第 2 步：登录 Boss 直聘（2 分钟）

```bash
boss login
```

终端会弹出一个二维码——用 **Boss 直聘手机 App** 扫码确认。成功后会显示 `✅ 已登录`。

> 如果你已经在 Chrome 里登录过 Boss 网页版，可以直接 `boss login`，工具会自动从浏览器导 cookie，不用扫码。

验证：

```bash
boss status              # 应该显示用户名和已登录状态
boss recruiter jobs      # 应该列出你的在招职位
```

---

## 第 3 步：写你的回复模板（3 分钟）

```bash
mkdir -p ~/.config/boss-cli
cp ~/dev/boss-cli/templates.txt.example ~/.config/boss-cli/templates.txt
open -e ~/.config/boss-cli/templates.txt   # 用文本编辑器打开
```

把里面的模板改成**你平时的口吻**，比如：

```
你好，方便发一份最新的简历吗？
你好，麻烦发份完整简历，我看完跟你约时间细聊
Hi，可以先把简历发我看看吗？
你好{name}，看完你打的招呼觉得挺合适，先发份简历过来呗
你好，简历方便先发一份吗？看完咱们详细聊
```

**规则**：
- 一行一条，`#` 开头是注释
- `{name}` 会自动替换为候选人姓名
- **至少写 8 条**（少了风控会觉得你像机器）
- **不要写**微信号、电话号、链接（Boss 会秒删消息）

存盘后验证：

```bash
boss recruiter templates       # 应该列出你写的所有模板
```

---

## 第 4 步：在终端试跑一次（2 分钟）

```bash
# 看看现在有谁等你回复（不发任何东西）
boss recruiter pending

# 干跑：显示"会发什么"但不真发
boss recruiter auto-reply --dry-run --max-send 3
```

如果输出看着对，再真发一次：

```bash
boss recruiter auto-reply --max-send 3
# 它会让你按 y 确认，回 3 条之间各间隔 12-30 秒
```

去 Boss 直聘 App 看消息列表——应该已经回出去了。✅

---

## 第 5 步：接到 Codex 桌面端（5 分钟）

### 方法 A：通过 GUI 添加（推荐）

1. 打开 **Codex 桌面 App**
2. 左下角点 **Settings**（设置）
3. 左边栏点 **MCP servers**
4. 点 **+ Add server**
5. 填：
   - **Name**: `boss`
   - **Type**: `STDIO`（默认就是）
   - **Command**: 粘贴第 1 步末尾让你记下的那个绝对路径，比如 `/Users/dingyuxuan/dev/boss-cli/.venv/bin/boss-cli-mcp`
   - **Args**: 留空
   - **Env**: 留空
6. 点 **Save**
7. **重启 Codex**

### 方法 B：直接改配置文件

```bash
mkdir -p ~/.codex
open -e ~/.codex/config.toml
```

在文件末尾追加（把 `command` 换成你的真实路径）：

```toml
[mcp_servers.boss]
command = "/Users/dingyuxuan/dev/boss-cli/.venv/bin/boss-cli-mcp"
```

存盘，**重启 Codex**。

### 验证

在 Codex 对话框里说：

> 你能用 boss 这个 MCP 看下我有哪些工具吗？

Codex 应该列出 `list_pending` / `auto_reply` / `list_templates` / `add_template` / `reset_templates` 这 5 个。

---

## 第 6 步：在 Codex 里实际使用

直接用大白话指挥 Codex，例如：

> 看下 boss 现在有几个候选人等我回复，先列出来给我看。

Codex 会调 `list_pending`，告诉你"有 7 个人等你回复"。然后你说：

> 帮我都回了，先 dry_run 看下会发什么。

Codex 会调 `auto_reply(dry_run=true)`，把"打算给谁发什么"的清单返回。你看完说：

> 可以，真发出去。

Codex 会调 `auto_reply(dry_run=false)`，按节奏一条条发。

**几个常用指令模板**：

| 你想做的事 | 跟 Codex 说 |
|---|---|
| 看待回复名单 | "看下 boss 有几个等回复的" |
| 干跑预览 | "boss 帮我自动回，先 dry run" |
| 真实发送 | "确认发出去" |
| 只针对某个职位 | "只看 encryptJobId 是 xxx 的候选人，帮我回了" |
| 加新模板 | "给 boss 模板池加一条：'你好，简历方便发下吗'" |
| 看现有模板 | "boss 现在有哪些模板？" |

---

## 第 7 步（可选）：接到其他 agent

### Claude Code (Anthropic)

```bash
claude mcp add boss /Users/dingyuxuan/dev/boss-cli/.venv/bin/boss-cli-mcp
```

### Claude Desktop App

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`，加：

```json
{
  "mcpServers": {
    "boss": {
      "command": "/Users/dingyuxuan/dev/boss-cli/.venv/bin/boss-cli-mcp"
    }
  }
}
```

重启 Claude Desktop。

### Workbuddy 或其他 MCP 客户端

通用规则：让客户端启动 `boss-cli-mcp` 命令，使用 **stdio** 协议。具体配在哪个文件、哪个菜单，请查该 app 自己的 MCP 文档关键词："**add MCP server**" 或 "**stdio command**"。基本上都是填一个 Name + 一个绝对路径，跟上面 Codex 的方法 A 是一回事。

---

## 故障排查

### "未登录" / 候选人列表是空的
```bash
boss logout && boss login    # 重新扫码
```

### Codex 找不到 boss 这个 MCP
- 确认配的是**绝对路径**（`/Users/.../boss-cli-mcp`），不是 `boss-cli-mcp`
- 重启 Codex（不是关窗口，要完全 Quit 再开）
- 在终端跑一遍 `boss-cli-mcp`，立即 Ctrl+C；如果没报错说明命令本身没坏

### 发消息时报 "code 9" / "需要 stoken"
账号被风控了。**立刻停止 24 小时**，不要重试。下次：
- 模板池再扩到 15 条以上
- `--max-send 3` 之类小批量分多次跑
- 错峰时段（避开 9 点整、10 点整这种太规整的时间）

### 不在工作时间报错
工具默认只在 9:30-12:00 / 14:00-19:00 跑。强行跑加 `--ignore-hours`，但不推荐——非工作时间发消息正是风控的重点信号。

### 想改默认上限/时段
编辑 `~/dev/boss-cli/boss_cli/auto_reply.py`，搜 `WORK_HOURS` / `DEFAULT_DAILY_QUOTA` / `SEND_DELAY_MIN`。改完不用重装。

---

## 安全和审计

每发一条消息都写一行到 `~/.config/boss-cli/auto_reply_audit.jsonl`。出问题时回看：

```bash
tail -20 ~/.config/boss-cli/auto_reply_audit.jsonl
```

今日已用配额：

```bash
cat ~/.config/boss-cli/auto_reply_quota.json
```

---

## 一句话总结

```
boss login            # 一次性
boss recruiter pending           # 看谁等你
boss recruiter auto-reply -y     # 帮你回
```

或在 Codex 里说："boss 看下有谁等我回，帮我都回了"。

---

> 本项目 fork 自 [jackwener/boss-cli](https://github.com/jackwener/boss-cli)，在原有 CLI 基础上加了 HR 自动回复 + MCP server 能力。完整的求职者端 / 招聘方其他命令请看 [README_UPSTREAM.md](README_UPSTREAM.md)。
