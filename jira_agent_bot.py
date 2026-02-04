"""
Telegram Jira Bot - Multi-Agent Architecture with Vision Support (Webhook Mode)
使用 OpenAI Function Calling 和 Vision API 实现的 BUG 提交机器人
采用 Webhook 模式提高稳定性
"""
import os
import json
import asyncio
import requests
import base64
from flask import Flask, request, Response
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# --- Environment Variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE")
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "BB")
PORT = int(os.getenv("PORT", 10000))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g., https://telegram-jira-bot-29j8.onrender.com

# --- Assignee IDs ---
ASSIGNEE_DEV = "712020:29364cb3-1ba1-453c-8e28-4e0306787939"
ASSIGNEE_UI = "712020:7b0eae8d-9cc3-406b-814e-bbbe51c67cbd"

# --- OpenAI Client ---
client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE) if OPENAI_API_BASE else OpenAI(api_key=OPENAI_API_KEY)

# --- Flask App for Webhook ---
app = Flask(__name__)

# --- Global Application ---
application = None


# ============================================================
# Vision API Functions
# ============================================================

def encode_image_to_base64(image_bytes: bytes) -> str:
    """将图片字节转换为 base64 字符串"""
    return base64.b64encode(image_bytes).decode('utf-8')


async def download_telegram_photo(bot: Bot, file_id: str) -> tuple:
    """下载 Telegram 图片"""
    try:
        file = await bot.get_file(file_id)
        file_path = file.file_path
        
        print(f"Telegram file_path: {file_path}")
        
        # 检查 file_path 是否已经是完整 URL
        if file_path.startswith("http"):
            download_url = file_path
        else:
            download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        
        print(f"Downloading from: {download_url}")
        
        response = requests.get(download_url)
        response.raise_for_status()
        
        filename = file_path.split('/')[-1] if '/' in file_path else f"photo_{file_id}.jpg"
        print(f"Photo downloaded successfully: {filename}, size: {len(response.content)} bytes")
        
        return response.content, filename
    except Exception as e:
        print(f"Error downloading photo: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def analyze_image_with_vision(image_base64: str, user_text: str) -> str:
    """使用 OpenAI Vision API 分析图片"""
    try:
        print("Calling Vision API to analyze image...")
        
        response = client.chat.completions.create(
            model="gpt-5.2",
            max_completion_tokens=1500,
            messages=[
                {
                    "role": "system",
                    "content": """你是一位专业的软件测试工程师和 UI/UX 专家。
请仔细分析用户提供的截图，结合用户的文字描述，提取以下信息：

1. **界面元素识别**：识别截图中的关键 UI 元素（按钮、文本、图标、布局等）
2. **问题定位**：根据用户描述，定位截图中可能存在问题的区域
3. **视觉问题**：识别任何明显的视觉问题（对齐、颜色、间距、遮挡等）
4. **交互问题**：推断可能的交互问题（按钮状态、反馈缺失等）

请用简洁专业的语言描述你的分析结果，为后续创建 BUG 报告提供依据。"""
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"用户描述：{user_text}\n\n请分析这张截图，识别问题所在。"
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ]
        )
        
        analysis = response.choices[0].message.content
        print(f"Vision analysis completed: {analysis[:100]}...")
        return analysis
        
    except Exception as e:
        error_msg = f"图片分析失败：{str(e)}"
        print(error_msg)
        return error_msg


# ============================================================
# Jira API Functions
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
        
        # 截断过长的标题
        if len(title) > 250:
            title = title[:247] + "..."
        
        # 将描述按换行符分割成多个段落，并限制长度
        MAX_PARA_LENGTH = 2000
        MAX_PARAGRAPHS = 50
        MAX_TOTAL_LENGTH = 30000
        
        if len(description) > MAX_TOTAL_LENGTH:
            description = description[:MAX_TOTAL_LENGTH] + "\n\n[描述已截断...]" 
        
        paragraphs = description.split('\n')
        content_blocks = []
        for para in paragraphs:
            if para.strip():
                if len(para) > MAX_PARA_LENGTH:
                    para = para[:MAX_PARA_LENGTH] + "..."
                content_blocks.append({
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": para}
                    ]
                })
                if len(content_blocks) >= MAX_PARAGRAPHS:
                    content_blocks.append({
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "[更多内容已省略...]"}
                        ]
                    })
                    break

        payload = {
            "fields": {
                "project": {"key": JIRA_PROJECT_KEY},
                "summary": title,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": content_blocks if content_blocks else [
                        {"type": "paragraph", "content": [{"type": "text", "text": description}]}
                    ]
                },
                "issuetype": {"name": "缺陷"},
                "assignee": {"accountId": assignee_id}
            }
        }

        print(f"Creating Jira issue with title: {title}")
        response = requests.post(url, headers=headers, auth=auth, data=json.dumps(payload))
        
        if response.status_code >= 400:
            print(f"Jira API error: {response.status_code} - {response.text}")
        
        response.raise_for_status()
        result = response.json()
        issue_key = result["key"]
        issue_url = f"https://{JIRA_DOMAIN}/browse/{issue_key}"
        
        print(f"Jira issue created: {issue_key}")
        
        return {
            "success": True,
            "issue_key": issue_key,
            "issue_url": issue_url,
            "title": title,
            "bug_type": bug_type
        }
        
    except requests.exceptions.HTTPError as e:
        error_msg = f"HTTP Error: {e.response.status_code} - {e.response.text}"
        print(f"Jira create error: {error_msg}")
        return {"success": False, "error": error_msg}
    except Exception as e:
        print(f"Jira create error: {e}")
        return {"success": False, "error": str(e)}


def upload_attachment_to_jira(issue_key: str, image_bytes: bytes, filename: str) -> dict:
    """上传附件到 Jira Issue"""
    try:
        url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{issue_key}/attachments"
        auth = (JIRA_EMAIL, JIRA_API_TOKEN)
        headers = {
            "Accept": "application/json",
            "X-Atlassian-Token": "no-check"
        }
        
        files = {
            "file": (filename, image_bytes, "image/jpeg")
        }
        
        print(f"Uploading attachment to {issue_key}...")
        response = requests.post(url, headers=headers, auth=auth, files=files)
        response.raise_for_status()
        
        print(f"Attachment uploaded successfully to {issue_key}")
        return {"success": True, "result": response.json()}
        
    except Exception as e:
        print(f"Attachment upload error: {e}")
        return {"success": False, "error": str(e)}


def search_jira_issues(query: str) -> dict:
    """搜索 Jira Issues"""
    try:
        url = f"https://{JIRA_DOMAIN}/rest/api/3/search"
        auth = (JIRA_EMAIL, JIRA_API_TOKEN)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        
        jql = f'project = {JIRA_PROJECT_KEY} AND (summary ~ "{query}" OR description ~ "{query}") ORDER BY created DESC'
        payload = {"jql": jql, "maxResults": 5, "fields": ["summary", "status", "created", "assignee"]}
        
        response = requests.post(url, headers=headers, auth=auth, data=json.dumps(payload))
        response.raise_for_status()
        
        issues = response.json().get("issues", [])
        results = []
        for issue in issues:
            results.append({
                "key": issue["key"],
                "summary": issue["fields"]["summary"],
                "status": issue["fields"]["status"]["name"],
                "url": f"https://{JIRA_DOMAIN}/browse/{issue['key']}"
            })
        
        return {"success": True, "issues": results, "count": len(results)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_jira_issue(issue_key: str) -> dict:
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
            "url": f"https://{JIRA_DOMAIN}/browse/{issue['key']}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================
# Agent System
# ============================================================

# 用户会话存储
user_sessions = {}
pending_images = {}

SYSTEM_PROMPT = """你是一位资深的 QA 工程师和产品专家，专门帮助用户高效提交 BUG 到 Jira。

## 核心原则
1. **一次完成**：尽量从用户描述中提取所有信息，不要反复追问
2. **专业分析**：为每个 BUG 提供技术根因分析
3. **修复建议**：给出具体可行的修复方向

## 工作流程
当用户报告问题时：
1. 快速理解问题本质
2. 自动补全缺失信息（基于经验推断）
3. 生成专业的 BUG 标题和描述
4. 直接调用工具创建 Jira Issue

## BUG 分类标准
- **开发BUG**：功能异常、逻辑错误、接口问题、性能问题、数据问题
- **UI/设计BUG**：样式问题、布局问题、动效问题、视觉不一致

## 描述模板
【问题描述】简明扼要描述问题现象
【复现步骤】1. 2. 3.
【预期结果】应该怎样
【实际结果】实际怎样
【影响范围】影响哪些用户/场景
【技术分析】可能的技术原因
【修复建议】建议的修复方向

## 注意事项
- 标题要简洁有力，包含模块名和问题关键词
- 如果用户提供了截图分析，要在描述中引用
- 不要问用户"还需要补充什么"，直接创建
- 创建成功后给出简洁的确认信息"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_jira_issue",
            "description": "创建 Jira BUG Issue。当用户报告了一个问题或 BUG 时调用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue 标题，简洁描述问题"},
                    "description": {"type": "string", "description": "详细的问题描述，包含复现步骤、预期结果、实际结果等"},
                    "bug_type": {"type": "string", "enum": ["开发BUG", "UI/设计BUG"], "description": "BUG 类型"}
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
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_jira_issue",
            "description": "获取指定 Jira Issue 的详情",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue 编号，如 BB-123"}
                },
                "required": ["issue_key"]
            }
        }
    }
]


def get_user_session(user_id: int) -> list:
    """获取用户会话历史"""
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    return user_sessions[user_id]


def add_to_session(user_id: int, role: str, content: str):
    """添加消息到会话"""
    session = get_user_session(user_id)
    session.append({"role": role, "content": content})
    if len(session) > 20:
        session.pop(0)


def clear_user_session(user_id: int):
    """清除用户会话"""
    if user_id in user_sessions:
        user_sessions[user_id] = []


def execute_tool(tool_name: str, arguments: dict, user_id: int) -> str:
    """执行工具调用"""
    print(f"执行工具: {tool_name}, 参数: {arguments}")
    
    if tool_name == "create_jira_issue":
        result = create_jira_issue(
            title=arguments.get("title", ""),
            description=arguments.get("description", ""),
            bug_type=arguments.get("bug_type", "开发BUG")
        )
        
        if result["success"]:
            # 如果有待上传的图片，上传到 Jira
            if user_id in pending_images:
                image_data = pending_images[user_id]
                upload_result = upload_attachment_to_jira(
                    result["issue_key"],
                    image_data["bytes"],
                    image_data["filename"]
                )
                del pending_images[user_id]
                
                if upload_result["success"]:
                    print(f"Attachment uploaded successfully to {result['issue_key']}")
            
            return f"✅ BUG 已提交到 Jira!\n\n📋 **Issue 信息**\n• 编号：{result['issue_key']}\n• 标题：{result['title']}\n• 类型：{result['bug_type']}\n\n🔗 {result['issue_url']}"
        else:
            return f"❌ Jira 创建失败：{result['error']}"
    
    elif tool_name == "search_jira_issues":
        result = search_jira_issues(arguments.get("query", ""))
        if result["success"]:
            if result["count"] == 0:
                return "未找到相关 Issue"
            issues_text = "\n".join([f"• [{i['key']}] {i['summary']} ({i['status']})" for i in result["issues"]])
            return f"🔍 找到 {result['count']} 个相关 Issue:\n{issues_text}"
        return f"搜索失败：{result['error']}"
    
    elif tool_name == "get_jira_issue":
        result = get_jira_issue(arguments.get("issue_key", ""))
        if result["success"]:
            return f"📋 **{result['key']}**\n• 标题：{result['summary']}\n• 状态：{result['status']}\n• 链接：{result['url']}"
        return f"获取失败：{result['error']}"
    
    return "未知工具"


async def run_agent(user_id: int, user_message: str, image_analysis: str = None) -> str:
    """运行 Agent 处理用户消息"""
    
    # 构建完整的用户消息
    full_message = user_message
    if image_analysis:
        full_message = f"{user_message}\n\n【截图分析】\n{image_analysis}"
    
    add_to_session(user_id, "user", full_message)
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + get_user_session(user_id)
    
    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto"
        )
        
        assistant_message = response.choices[0].message
        
        # 处理工具调用
        if assistant_message.tool_calls:
            tool_results = []
            for tool_call in assistant_message.tool_calls:
                tool_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)
                result = execute_tool(tool_name, arguments, user_id)
                tool_results.append(result)
            
            final_response = "\n\n".join(tool_results)
            add_to_session(user_id, "assistant", final_response)
            return final_response
        else:
            content = assistant_message.content or "我理解了你的问题，请提供更多细节。"
            add_to_session(user_id, "assistant", content)
            return content
            
    except Exception as e:
        error_msg = f"处理失败：{str(e)}"
        print(f"Agent error: {e}")
        import traceback
        traceback.print_exc()
        return error_msg


# ============================================================
# Telegram Handlers
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    welcome_message = """
👋 **欢迎使用 Jira BUG 提交助手！**

🎯 **我能做什么：**
• 帮你分析和提交 BUG 到 Jira
• 支持截图识别，自动分析界面问题
• 搜索已有的 Issue
• 查看 Issue 详情

📝 **如何使用：**
在群里 @我 并描述你遇到的问题，例如：
`@jira9527bot 登录页面点击按钮没有反应`

📷 **支持截图：**
发送截图并 @我 描述问题，我会自动分析截图内容！

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

**支持截图：**
发送截图并 @我 描述问题，我会自动分析截图内容并创建 Issue。

**示例：**
• `@jira9527bot 首页加载很慢，需要5秒以上`
• `@jira9527bot 用户头像显示不出来，一直是默认图片`
• 发送截图 + `@jira9527bot 这个按钮点击没反应`

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
    """处理用户消息（包括图片）"""
    if not update.message:
        return

    bot_username = (await context.bot.get_me()).username
    
    # 获取消息文本和图片
    message_text = update.message.text or update.message.caption or ""
    photo = update.message.photo[-1] if update.message.photo else None
    
    # 只在被 @ 时触发
    is_mentioned = f"@{bot_username}" in message_text
    
    if not is_mentioned:
        return

    user_id = update.effective_user.id
    user_message = message_text.replace(f"@{bot_username}", "").strip()
    
    if not user_message and not photo:
        await update.message.reply_text("请告诉我你遇到了什么问题？可以附上截图。")
        return
    
    # 发送"正在处理"提示
    if photo:
        processing_msg = await update.message.reply_text("🖼️ 正在分析截图和问题...")
    else:
        processing_msg = await update.message.reply_text("🤔 正在分析你的问题...")

    try:
        image_analysis = None
        
        # 如果有图片，下载并分析
        if photo:
            print(f"Processing photo: {photo.file_id}")
            
            # 下载图片
            image_bytes, filename = await download_telegram_photo(context.bot, photo.file_id)
            
            if image_bytes:
                print(f"Photo downloaded: {filename}, {len(image_bytes)} bytes")
                
                # 存储图片用于后续上传到 Jira
                pending_images[user_id] = {
                    "bytes": image_bytes,
                    "filename": filename
                }
                
                # 使用 Vision API 分析图片
                image_base64 = encode_image_to_base64(image_bytes)
                image_analysis = analyze_image_with_vision(image_base64, user_message or "请分析这张截图中的问题")
                
                print(f"Image analysis completed")
            else:
                print("Failed to download photo")
                image_analysis = "截图下载失败，将仅根据文字描述处理。"
        
        # 运行 Agent
        reply = await run_agent(user_id, user_message or "请根据截图分析问题", image_analysis)
        
        # 删除"正在处理"消息
        await processing_msg.delete()
        
        # 发送回复
        await update.message.reply_text(reply)

    except Exception as e:
        error_message = str(e)
        print(f"Error processing message: {error_message}")
        import traceback
        traceback.print_exc()
        
        try:
            await processing_msg.delete()
        except:
            pass
        
        await update.message.reply_text(f"❌ 处理失败：{error_message}")


# ============================================================
# Webhook Routes
# ============================================================

@app.route('/', methods=['GET'])
def health_check():
    """健康检查端点"""
    return 'OK - Telegram Jira Agent Bot with Vision is running (Webhook Mode)'


@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook 端点，接收 Telegram 更新"""
    global application
    
    if application is None:
        return Response('Application not initialized', status=500)
    
    try:
        update_data = request.get_json(force=True)
        update = Update.de_json(update_data, application.bot)
        
        # 异步处理更新
        asyncio.run(application.process_update(update))
        
        return Response('OK', status=200)
    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return Response('Error', status=500)


def setup_webhook():
    """设置 Telegram Webhook"""
    if not WEBHOOK_URL:
        print("Warning: WEBHOOK_URL not set, webhook will not be configured")
        return False
    
    webhook_url = f"{WEBHOOK_URL}/webhook"
    
    # 先删除旧的 webhook
    delete_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
    try:
        response = requests.get(delete_url)
        print(f"Delete webhook response: {response.json()}")
    except Exception as e:
        print(f"Error deleting webhook: {e}")
    
    # 设置新的 webhook
    set_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    payload = {
        "url": webhook_url,
        "drop_pending_updates": True,
        "allowed_updates": ["message", "edited_message"]
    }
    
    try:
        response = requests.post(set_url, json=payload)
        result = response.json()
        print(f"Set webhook response: {result}")
        return result.get("ok", False)
    except Exception as e:
        print(f"Error setting webhook: {e}")
        return False


def init_application():
    """初始化 Telegram Application"""
    global application
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # 添加处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_message))
    
    # 初始化 application
    asyncio.get_event_loop().run_until_complete(application.initialize())
    
    print("Telegram Application initialized")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("Starting Jira Agent Bot in Webhook Mode...")
    
    # 初始化 Telegram Application
    init_application()
    
    # 设置 Webhook
    if setup_webhook():
        print("Webhook configured successfully")
    else:
        print("Warning: Webhook configuration failed or WEBHOOK_URL not set")
    
    # 启动 Flask 服务器
    print(f"Starting Flask server on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT, debug=False)
"""
