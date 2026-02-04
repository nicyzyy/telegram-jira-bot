"""
Telegram Jira Bot - Multi-Agent Architecture
使用 OpenAI Function Calling 实现的 BUG 提交机器人
"""
import os
import json
import asyncio
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# --- Environment Variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "BB")
PORT = int(os.getenv("PORT", 10000))

# --- Assignee IDs ---
ASSIGNEE_DEV = "712020:29364cb3-1ba1-453c-8e28-4e0306787939"
ASSIGNEE_UI = "712020:7b0eae8d-9cc3-406b-814e-bbbe51c67cbd"

# --- OpenAI Client ---
client = OpenAI(api_key=OPENAI_API_KEY)

# --- Health Check HTTP Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK - Telegram Jira Agent Bot is running')
    
    def log_message(self, format, *args):
        pass

def start_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    print(f"Health check server running on port {PORT}")
    server.serve_forever()


# ============================================================
# Jira 工具函数
# ============================================================

def create_jira_issue(title: str, description: str, bug_type: str) -> dict:
    """创建 Jira Issue"""
    try:
        url = f"https://{JIRA_DOMAIN}/rest/api/3/issue"
        auth = (JIRA_EMAIL, JIRA_API_TOKEN)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        assignee_id = ASSIGNEE_DEV if "开发" in bug_type else ASSIGNEE_UI

        payload = {
            "fields": {
                "project": {"key": JIRA_PROJECT_KEY},
                "summary": title,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": description}
                            ]
                        }
                    ]
                },
                "issuetype": {"name": "缺陷"},
                "assignee": {"accountId": assignee_id}
            }
        }

        response = requests.post(url, headers=headers, auth=auth, data=json.dumps(payload))
        response.raise_for_status()
        result = response.json()
        issue_key = result["key"]
        issue_url = f"https://{JIRA_DOMAIN}/browse/{issue_key}"
        
        return {
            "success": True,
            "issue_key": issue_key,
            "issue_url": issue_url,
            "title": title,
            "bug_type": bug_type
        }
        
    except Exception as e:
        return {"success": False, "error": str(e)}


def search_jira_issues(query: str) -> dict:
    """搜索 Jira Issues"""
    try:
        url = f"https://{JIRA_DOMAIN}/rest/api/3/search"
        auth = (JIRA_EMAIL, JIRA_API_TOKEN)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        
        jql = f'project = {JIRA_PROJECT_KEY} AND text ~ "{query}" ORDER BY created DESC'
        params = {"jql": jql, "maxResults": 5, "fields": ["summary", "status", "created"]}
        
        response = requests.get(url, headers=headers, auth=auth, params=params)
        response.raise_for_status()
        result = response.json()
        
        issues = []
        for issue in result.get("issues", []):
            issues.append({
                "key": issue["key"],
                "summary": issue["fields"]["summary"],
                "status": issue["fields"]["status"]["name"],
                "url": f"https://{JIRA_DOMAIN}/browse/{issue['key']}"
            })
        
        return {"success": True, "count": len(issues), "issues": issues}
        
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_jira_issue_detail(issue_key: str) -> dict:
    """获取 Jira Issue 详情"""
    try:
        url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}"
        auth = (JIRA_EMAIL, JIRA_API_TOKEN)
        headers = {"Accept": "application/json"}
        
        response = requests.get(url, headers=headers, auth=auth)
        response.raise_for_status()
        issue = response.json()
        
        return {
            "success": True,
            "key": issue["key"],
            "summary": issue["fields"]["summary"],
            "status": issue["fields"]["status"]["name"],
            "assignee": issue["fields"].get("assignee", {}).get("displayName", "未分配"),
            "created": issue["fields"]["created"],
            "url": f"https://{JIRA_DOMAIN}/browse/{issue['key']}"
        }
        
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================
# OpenAI Function Calling 定义
# ============================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_jira_issue",
            "description": "创建一个新的 Jira Issue（BUG）。在收集到足够的信息后调用此函数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "BUG 标题，简洁明了地描述问题"
                    },
                    "description": {
                        "type": "string",
                        "description": "BUG 详细描述，包含复现步骤、预期结果、实际结果等"
                    },
                    "bug_type": {
                        "type": "string",
                        "enum": ["开发BUG", "UI/设计BUG"],
                        "description": "BUG 类型：开发BUG（功能问题）或 UI/设计BUG（界面问题）"
                    }
                },
                "required": ["title", "description", "bug_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_jira_issues",
            "description": "搜索已有的 Jira Issues",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_jira_issue_detail",
            "description": "获取特定 Jira Issue 的详细信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue 编号，如 BB-123"
                    }
                },
                "required": ["issue_key"]
            }
        }
    }
]

SYSTEM_PROMPT = """
你是一个专业的 BUG 管理助手，名叫 "Jira小助手"。你的职责是帮助用户提交和管理 BUG。

## 核心能力
1. **分析 BUG 报告**：理解用户描述的问题，提取关键信息
2. **创建 Jira Issue**：将 BUG 提交到 Jira 系统
3. **搜索 Issue**：帮助用户查找已有的 Issue
4. **查看 Issue 详情**：获取特定 Issue 的详细信息

## 工作流程

### 当用户报告 BUG 时：
1. 分析用户的描述，提取以下信息：
   - BUG 标题（简洁明了）
   - BUG 描述（包含复现步骤、预期结果、实际结果）
   - BUG 类型（开发BUG 或 UI/设计BUG）

2. 如果信息不完整，主动询问用户：
   - "请问这个问题是在什么场景下发生的？"
   - "能否描述一下复现步骤？"
   - "这是功能问题还是界面问题？"

3. 信息完整后，调用 create_jira_issue 函数创建 Issue

4. 向用户确认创建结果，提供 Issue 链接

### 当用户查询 Issue 时：
- 使用 search_jira_issues 搜索相关 Issue
- 使用 get_jira_issue_detail 获取详情

## 回复风格
- 使用中文回复
- 友好、专业
- 使用 emoji 增加可读性（如 ✅ 📋 🔗 ❌）
- 回复要简洁，避免冗长

## 注意事项
- 如果用户的描述太模糊，一定要追问细节
- 创建 Issue 前确保有足够的信息
- 出错时给出清晰的错误说明
"""


# ============================================================
# Agent 核心逻辑
# ============================================================

# 用户会话存储
user_sessions = {}

def get_user_session(user_id: int) -> list:
    """获取用户的对话历史"""
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    return user_sessions[user_id]

def add_to_session(user_id: int, message: dict):
    """添加消息到会话历史"""
    session = get_user_session(user_id)
    session.append(message)
    # 保留最近 20 条消息
    if len(session) > 20:
        user_sessions[user_id] = session[-20:]

def clear_user_session(user_id: int):
    """清除用户会话"""
    if user_id in user_sessions:
        del user_sessions[user_id]


def execute_tool(tool_name: str, arguments: dict) -> str:
    """执行工具函数"""
    if tool_name == "create_jira_issue":
        result = create_jira_issue(
            title=arguments.get("title"),
            description=arguments.get("description"),
            bug_type=arguments.get("bug_type")
        )
    elif tool_name == "search_jira_issues":
        result = search_jira_issues(query=arguments.get("query"))
    elif tool_name == "get_jira_issue_detail":
        result = get_jira_issue_detail(issue_key=arguments.get("issue_key"))
    else:
        result = {"error": f"未知工具: {tool_name}"}
    
    return json.dumps(result, ensure_ascii=False)


async def run_agent(user_id: int, user_message: str) -> str:
    """运行 Agent 处理用户消息"""
    
    # 添加用户消息到会话
    add_to_session(user_id, {"role": "user", "content": user_message})
    
    # 构建消息列表
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(get_user_session(user_id))
    
    # 最多循环 5 次（防止无限循环）
    for _ in range(5):
        # 调用 OpenAI API
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto"
        )
        
        assistant_message = response.choices[0].message
        
        # 如果没有工具调用，返回文本回复
        if not assistant_message.tool_calls:
            reply = assistant_message.content
            add_to_session(user_id, {"role": "assistant", "content": reply})
            return reply
        
        # 处理工具调用
        messages.append({
            "role": "assistant",
            "content": assistant_message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                }
                for tc in assistant_message.tool_calls
            ]
        })
        
        # 执行每个工具调用
        for tool_call in assistant_message.tool_calls:
            tool_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            
            print(f"执行工具: {tool_name}, 参数: {arguments}")
            
            tool_result = execute_tool(tool_name, arguments)
            
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": tool_result
            })
    
    # 如果循环结束还没有返回，返回最后的消息
    final_response = client.chat.completions.create(
        model="gpt-5.2",
        messages=messages
    )
    
    reply = final_response.choices[0].message.content
    add_to_session(user_id, {"role": "assistant", "content": reply})
    return reply


# ============================================================
# Telegram Handlers
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    welcome_message = """
👋 你好！我是 **Jira小助手**，一个智能 BUG 管理机器人。

🎯 **我能做什么：**
• 帮你分析和提交 BUG 到 Jira
• 搜索已有的 Issue
• 查看 Issue 详情

📝 **如何使用：**
在群里 @我 并描述你遇到的问题，例如：
`@jira9527bot 登录页面点击按钮没有反应`

💡 **其他命令：**
• /clear - 清除对话历史
• /help - 查看帮助信息

有问题随时 @我！
"""
    await update.message.reply_text(welcome_message, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令"""
    help_message = """
📖 **使用帮助**

**报告 BUG：**
@我并描述问题，我会帮你分析并创建 Jira Issue。

**示例：**
• `@jira9527bot 首页加载很慢，需要5秒以上`
• `@jira9527bot 用户头像显示不出来，一直是默认图片`

**搜索 Issue：**
• `@jira9527bot 搜索登录相关的问题`
• `@jira9527bot 查看 BB-123 的详情`

**其他命令：**
• /clear - 清除对话历史，开始新对话
• /start - 查看欢迎信息
"""
    await update.message.reply_text(help_message, parse_mode="Markdown")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /clear 命令"""
    user_id = update.effective_user.id
    clear_user_session(user_id)
    await update.message.reply_text("✅ 对话历史已清除，我们可以开始新的对话了！")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户消息"""
    if not update.message or not update.message.text:
        return

    bot_username = (await context.bot.get_me()).username
    
    # 只在被 @ 时触发
    is_mentioned = f"@{bot_username}" in update.message.text
    
    if not is_mentioned:
        return

    user_id = update.effective_user.id
    user_message = update.message.text.replace(f"@{bot_username}", "").strip()
    
    if not user_message:
        await update.message.reply_text("请告诉我你遇到了什么问题？")
        return
    
    # 发送"正在处理"提示
    processing_msg = await update.message.reply_text("🤔 正在分析你的问题...")

    try:
        # 运行 Agent
        reply = await run_agent(user_id, user_message)
        
        # 删除"正在处理"消息
        await processing_msg.delete()
        
        # 发送回复
        await update.message.reply_text(reply)

    except Exception as e:
        error_message = str(e)
        print(f"Error processing message: {error_message}")
        
        try:
            await processing_msg.delete()
        except:
            pass
        
        await update.message.reply_text(f"❌ 处理失败：{error_message}")


# ============================================================
# Main Function
# ============================================================

def main():
    # 启动健康检查服务器
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    
    # 删除现有 webhook 避免冲突
    print("Deleting any existing webhook...")
    delete_webhook_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
    try:
        response = requests.get(delete_webhook_url)
        print(f"Webhook deleted: {response.json()}")
    except Exception as e:
        print(f"Warning: Could not delete webhook: {e}")
    
    # 创建 Telegram 应用
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # 添加处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Jira Agent Bot is running...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
