"""
Telegram Jira Bot - Multi-Agent Architecture with Vision Support (Pure Webhook Mode)
使用 OpenAI Function Calling 和 Vision API 实现的 BUG 提交机器人
采用纯 Webhook 模式，添加消息去重和异步处理机制
v3: 优化用户体验 - 即时反馈 + 精简回复
"""
import os
import sys
import json
import requests
import base64
import threading
import time
import logging
from collections import OrderedDict
from flask import Flask, request, Response
from openai import OpenAI

# 配置 logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# 确保 stdout 不缓冲
os.environ['PYTHONUNBUFFERED'] = '1'

# --- Environment Variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE")
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "BB")
PORT = int(os.getenv("PORT", 10000))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# --- Assignee IDs ---
ASSIGNEE_DEV = "712020:29364cb3-1ba1-453c-8e28-4e0306787939"
ASSIGNEE_UI = "712020:7b0eae8d-9cc3-406b-814e-bbbe51c67cbd"

# --- OpenAI Client ---
client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE) if OPENAI_API_BASE else OpenAI(api_key=OPENAI_API_KEY)

# --- Flask App for Webhook ---
app = Flask(__name__)

# --- User Sessions and Pending Images ---
user_sessions = {}
pending_images = {}

# --- Bot Username Cache ---
bot_username_cache = None

# --- Message Deduplication Cache (LRU with TTL) ---
class MessageCache:
    """消息去重缓存，使用 LRU + TTL 策略"""
    def __init__(self, max_size=1000, ttl_seconds=300):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl_seconds
        self.lock = threading.Lock()
    
    def is_duplicate(self, update_id: int) -> bool:
        """检查是否是重复消息"""
        with self.lock:
            current_time = time.time()
            
            # 清理过期条目
            expired_keys = [k for k, v in self.cache.items() if current_time - v > self.ttl]
            for k in expired_keys:
                del self.cache[k]
            
            # 检查是否存在
            if update_id in self.cache:
                return True
            
            # 添加新条目
            self.cache[update_id] = current_time
            
            # 如果超过最大容量，删除最旧的
            while len(self.cache) > self.max_size:
                self.cache.popitem(last=False)
            
            return False

# 全局消息缓存
message_cache = MessageCache()

# --- Processing Status Cache ---
processing_messages = {}
processing_lock = threading.Lock()


# ============================================================
# Telegram API Helper Functions
# ============================================================

def telegram_api(method: str, data: dict = None) -> dict:
    """调用 Telegram Bot API"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        if data:
            response = requests.post(url, json=data, timeout=30)
        else:
            response = requests.get(url, timeout=30)
        return response.json()
    except Exception as e:
        logger.error(f"Telegram API error: {e}")
        return {"ok": False, "error": str(e)}


def send_message(chat_id: int, text: str, reply_to_message_id: int = None, parse_mode: str = "Markdown") -> dict:
    """发送消息"""
    # 如果文本太长，截断
    if len(text) > 4000:
        text = text[:3997] + "..."
    
    data = {
        "chat_id": chat_id,
        "text": text
    }
    # 只有当 parse_mode 有值时才添加到请求中
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    
    result = telegram_api("sendMessage", data)
    
    # 如果 Markdown 解析失败，尝试不带格式发送
    if not result.get("ok") and "parse" in str(result.get("description", "")).lower():
        data.pop("parse_mode", None)  # 移除 parse_mode
        result = telegram_api("sendMessage", data)
    
    return result


def edit_message(chat_id: int, message_id: int, text: str, parse_mode: str = "Markdown") -> dict:
    """编辑消息"""
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text
    }
    # 只有当 parse_mode 有值时才添加到请求中
    if parse_mode:
        data["parse_mode"] = parse_mode
    
    result = telegram_api("editMessageText", data)
    
    # 如果 Markdown 解析失败，尝试不带格式
    if not result.get("ok") and "parse" in str(result.get("description", "")).lower():
        data.pop("parse_mode", None)  # 移除 parse_mode
        result = telegram_api("editMessageText", data)
    
    return result


def delete_message(chat_id: int, message_id: int) -> dict:
    """删除消息"""
    return telegram_api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def get_bot_username() -> str:
    """获取机器人用户名"""
    global bot_username_cache
    if bot_username_cache is None:
        result = telegram_api("getMe")
        if result.get("ok"):
            bot_username_cache = result["result"]["username"]
    return bot_username_cache


def download_file(file_id: str) -> tuple:
    """下载 Telegram 文件"""
    try:
        # 获取文件路径
        result = telegram_api("getFile", {"file_id": file_id})
        if not result.get("ok"):
            return None, None
        
        file_path = result["result"]["file_path"]
        
        # 下载文件
        if file_path.startswith("http"):
            download_url = file_path
        else:
            download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        
        response = requests.get(download_url, timeout=60)
        response.raise_for_status()
        
        filename = file_path.split('/')[-1] if '/' in file_path else f"photo_{file_id}.jpg"
        logger.info(f"Photo downloaded: {filename}, {len(response.content)} bytes")
        
        return response.content, filename
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return None, None


# ============================================================
# Vision API Functions
# ============================================================

def encode_image_to_base64(image_bytes: bytes) -> str:
    """将图片字节转换为 base64 字符串"""
    return base64.b64encode(image_bytes).decode('utf-8')


def analyze_image_with_vision(image_base64: str, user_text: str) -> str:
    """使用 OpenAI Vision API 分析图片"""
    try:
        logger.info("Calling Vision API to analyze image...")
        
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
        logger.info(f"Vision analysis completed: {analysis[:100]}...")
        return analysis
        
    except Exception as e:
        error_msg = f"图片分析失败：{str(e)}"
        logger.error(error_msg)
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
        
        # 添加类型前缀到标题
        if "开发" in bug_type:
            prefix = "【开发】"
        else:
            prefix = "【UI】"
        
        # 如果标题已经有类似前缀，不重复添加
        if not title.startswith("【"):
            title = prefix + title
        
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

        logger.info(f"Creating Jira issue with title: {title}")
        response = requests.post(url, headers=headers, auth=auth, data=json.dumps(payload), timeout=30)
        
        if response.status_code >= 400:
            logger.error(f"Jira API error: {response.status_code} - {response.text}")
        
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
        response = requests.post(url, headers=headers, auth=auth, files=files, timeout=60)
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
        
        response = requests.post(url, headers=headers, auth=auth, data=json.dumps(payload), timeout=30)
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
        
        response = requests.get(url, headers=headers, auth=auth, timeout=30)
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
# OpenAI Function Calling Tools
# ============================================================

tools = [
    {
        "type": "function",
        "function": {
            "name": "create_jira_issue",
            "description": "创建一个新的 Jira BUG Issue。当用户报告问题或 BUG 时调用此函数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Issue 标题，简洁描述问题（不超过100字）"
                    },
                    "description": {
                        "type": "string",
                        "description": "详细的 BUG 描述，包括：问题现象、复现步骤、预期行为、实际行为、可能原因、修复建议"
                    },
                    "bug_type": {
                        "type": "string",
                        "enum": ["开发BUG", "UI/UX问题"],
                        "description": "BUG 类型：功能性问题选'开发BUG'，界面/交互问题选'UI/UX问题'"
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
            "description": "搜索已存在的 Jira Issues。当用户想查找相关问题时调用。",
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
            "description": "获取指定 Jira Issue 的详细信息。当用户想查看某个 Issue 详情时调用。",
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


# ============================================================
# Session Management
# ============================================================

def get_user_session(user_id: int) -> list:
    """获取用户会话历史"""
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    return user_sessions[user_id]


def add_to_session(user_id: int, role: str, content: str):
    """添加消息到会话"""
    session = get_user_session(user_id)
    session.append({"role": role, "content": content})
    # 保持最近10条消息
    if len(session) > 10:
        user_sessions[user_id] = session[-10:]


def clear_user_session(user_id: int):
    """清除用户会话"""
    if user_id in user_sessions:
        del user_sessions[user_id]
    if user_id in pending_images:
        del pending_images[user_id]


# ============================================================
# Agent Logic
# ============================================================

def run_agent(user_id: int, user_message: str, image_analysis: str = None) -> dict:
    """
    运行 Agent 处理用户消息
    返回: {"reply": str, "jira_result": dict or None}
    """
    try:
        # 构建消息
        if image_analysis:
            full_message = f"用户描述：{user_message}\n\n截图分析结果：\n{image_analysis}"
        else:
            full_message = user_message
        
        add_to_session(user_id, "user", full_message)
        
        system_prompt = """你是一位专业的软件测试工程师和 BUG 分析专家。你的任务是：

1. **分析用户报告的问题**：理解问题的本质，判断是功能BUG还是UI问题
2. **直接创建 Issue**：不要反复询问，根据用户描述直接创建 Jira Issue
3. **专业的 BUG 描述**：生成结构化的 BUG 报告，包括：
   - 问题现象
   - 复现步骤（如果可以推断）
   - 预期行为 vs 实际行为
   - 可能的根本原因分析
   - 建议的修复方向

4. **智能分类**：
   - 功能异常、数据错误、性能问题 → 开发BUG
   - 界面显示、交互体验、视觉问题 → UI/UX问题

5. **如果用户提供了截图分析**：结合截图内容丰富 BUG 描述

请直接行动，高效处理用户的问题。"""

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(get_user_session(user_id))
        
        # 调用 OpenAI
        logger.info(f"Calling OpenAI API for agent decision...")
        try:
            response = client.chat.completions.create(
                model="gpt-5",
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_completion_tokens=2000,
                timeout=120  # 增加超时时间
            )
            logger.info(f"OpenAI API response received")
        except Exception as api_error:
            logger.error(f"OpenAI API call failed: {api_error}")
            raise
        
        assistant_message = response.choices[0].message
        logger.info(f"Assistant message: tool_calls={assistant_message.tool_calls is not None}, content={assistant_message.content[:50] if assistant_message.content else 'None'}...")
        
        jira_result = None
        
        # 处理工具调用
        if assistant_message.tool_calls:
            tool_results = []
            
            for tool_call in assistant_message.tool_calls:
                function_name = tool_call.function.name
                function_args = json.loads(tool_call.function.arguments)
                
                logger.info(f"Tool call: {function_name} with args: {function_args}")
                
                if function_name == "create_jira_issue":
                    result = create_jira_issue(
                        function_args["title"],
                        function_args["description"],
                        function_args["bug_type"]
                    )
                    
                    # 如果有待上传的图片，上传到 Issue
                    if result.get("success") and user_id in pending_images:
                        image_data = pending_images[user_id]
                        upload_result = upload_attachment_to_jira(
                            result["issue_key"],
                            image_data["bytes"],
                            image_data["filename"]
                        )
                        if upload_result.get("success"):
                            result["attachment_uploaded"] = True
                        del pending_images[user_id]
                    
                    jira_result = result
                    tool_results.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(result, ensure_ascii=False)
                    })
                    
                elif function_name == "search_jira_issues":
                    result = search_jira_issues(function_args["query"])
                    tool_results.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(result, ensure_ascii=False)
                    })
                    
                elif function_name == "get_jira_issue_detail":
                    result = get_jira_issue(function_args["issue_key"])
                    tool_results.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(result, ensure_ascii=False)
                    })
            
            # 将工具结果发送回 OpenAI 获取最终回复
            messages.append({"role": "assistant", "content": None, "tool_calls": assistant_message.tool_calls})
            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "content": tr["output"]
                })
            
            final_response = client.chat.completions.create(
                model="gpt-5",
                messages=messages,
                max_completion_tokens=1000
            )
            
            reply = final_response.choices[0].message.content
        else:
            reply = assistant_message.content
        
        add_to_session(user_id, "assistant", reply)
        return {"reply": reply, "jira_result": jira_result}
        
    except Exception as e:
        error_msg = f"Agent 处理失败：{str(e)}"
        logger.error(error_msg)
        import traceback
        traceback.print_exc()
        return {"reply": error_msg, "jira_result": None}


# ============================================================
# Message Handlers
# ============================================================

def handle_start_command(chat_id: int, message_id: int):
    """处理 /start 命令"""
    welcome_message = """👋 欢迎使用 Jira BUG 提交助手！

🎯 我能做什么：
• 帮你分析和提交 BUG 到 Jira
• 支持截图识别，自动分析界面问题
• 搜索已有的 Issue
• 查看 Issue 详情

📝 如何使用：
在群里 @我 并描述你遇到的问题，例如：
@jira9527bot 登录页面点击按钮没有反应

📷 支持截图：
发送截图并 @我 描述问题，我会自动分析截图内容！

💡 其他命令：
• /clear - 清除对话历史
• /help - 查看帮助信息

有问题随时 @我！"""
    send_message(chat_id, welcome_message, message_id, parse_mode=None)


def handle_help_command(chat_id: int, message_id: int):
    """处理 /help 命令"""
    help_message = """📖 使用帮助

报告 BUG：
@我并描述问题，我会帮你分析并创建 Jira Issue。

支持截图：
发送截图并 @我 描述问题，我会自动分析截图内容并创建 Issue。

示例：
• @jira9527bot 首页加载很慢，需要5秒以上
• @jira9527bot 用户头像显示不出来，一直是默认图片
• 发送截图 + @jira9527bot 这个按钮点击没反应

搜索 Issue：
• @jira9527bot 搜索登录相关的问题
• @jira9527bot 查看 BB-123 的详情

其他命令：
• /clear - 清除对话历史，开始新对话
• /start - 查看欢迎信息"""
    send_message(chat_id, help_message, message_id, parse_mode=None)


def handle_clear_command(chat_id: int, message_id: int, user_id: int):
    """处理 /clear 命令"""
    clear_user_session(user_id)
    send_message(chat_id, "✅ 对话历史已清除，我们可以开始新的对话了！", message_id, parse_mode=None)


def format_jira_success_message(jira_result: dict) -> str:
    """格式化 Jira 创建成功的精简消息"""
    issue_key = jira_result.get("issue_key", "")
    issue_url = jira_result.get("issue_url", "")
    title = jira_result.get("title", "")
    bug_type = jira_result.get("bug_type", "")
    has_attachment = jira_result.get("attachment_uploaded", False)
    
    # 截断过长的标题
    if len(title) > 50:
        title = title[:47] + "..."
    
    # 构建精简消息
    lines = [
        f"✅ 已创建 Jira Issue: **{issue_key}**",
        f"📋 {title}",
        f"🏷️ 类型: {bug_type}",
    ]
    
    if has_attachment:
        lines.append("📎 截图已附加")
    
    lines.append(f"🔗 {issue_url}")
    
    return "\n".join(lines)


def process_message_async(chat_id: int, message_id: int, user_id: int, text: str, photo_file_id: str = None, status_msg_id: int = None):
    """异步处理用户消息（即时反馈已在主线程发送）"""
    try:
        logger.info(f"[Async] Starting to process message: text='{text[:50] if text else ''}...', photo={photo_file_id is not None}, status_msg_id={status_msg_id}")
        
        bot_username = get_bot_username()
        user_message = text.replace(f"@{bot_username}", "").strip()
        
        if not user_message and not photo_file_id:
            if status_msg_id:
                edit_message(chat_id, status_msg_id, "请告诉我你遇到了什么问题？可以附上截图。", parse_mode=None)
            else:
                send_message(chat_id, "请告诉我你遇到了什么问题？可以附上截图。", message_id, parse_mode=None)
            return
        
        image_analysis = None
        
        # 如果有图片，下载并分析
        if photo_file_id:
            logger.info(f"Processing photo: {photo_file_id}")
            
            # 更新状态：正在下载图片
            if status_msg_id:
                edit_message(chat_id, status_msg_id, "📸 正在下载截图...", parse_mode=None)
            
            image_bytes, filename = download_file(photo_file_id)
            
            if image_bytes:
                # 存储图片用于后续上传到 Jira
                pending_images[user_id] = {
                    "bytes": image_bytes,
                    "filename": filename
                }
                
                # 更新状态：正在分析图片
                if status_msg_id:
                    edit_message(chat_id, status_msg_id, "🔍 正在分析截图内容...", parse_mode=None)
                
                # 使用 Vision API 分析图片
                image_base64 = encode_image_to_base64(image_bytes)
                image_analysis = analyze_image_with_vision(image_base64, user_message or "请分析这张截图中的问题")
            else:
                image_analysis = "截图下载失败，将仅根据文字描述处理。"
        
        # 更新状态：正在创建 Issue
        if status_msg_id:
            edit_message(chat_id, status_msg_id, "⏳ 正在创建 Jira Issue...", parse_mode=None)
        
        # 运行 Agent
        result = run_agent(user_id, user_message or "请根据截图分析问题", image_analysis)
        
        # 删除状态消息
        if status_msg_id:
            delete_message(chat_id, status_msg_id)
        
        # 发送最终回复
        jira_result = result.get("jira_result")
        
        if jira_result and jira_result.get("success"):
            # Jira 创建成功，发送精简消息
            reply = format_jira_success_message(jira_result)
            send_message(chat_id, reply, message_id)
        else:
            # 其他情况（搜索、查询、或失败），使用原始回复
            reply = result.get("reply", "处理完成")
            # 如果回复太长，截断
            if len(reply) > 500:
                reply = reply[:497] + "..."
            send_message(chat_id, reply, message_id)
        
    except Exception as e:
        error_message = str(e)
        logger.error(f"Error processing message: {error_message}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        if status_msg_id:
            delete_message(chat_id, status_msg_id)
        
        send_message(chat_id, f"❌ 处理失败：{error_message}", message_id, parse_mode=None)


def handle_user_message(chat_id: int, message_id: int, user_id: int, text: str, photo_file_id: str = None):
    """处理用户消息 - 先发送即时反馈，然后启动异步处理线程"""
    # 检查是否 @ 了机器人
    bot_username = get_bot_username()
    logger.info(f"Checking bot mention: bot_username={bot_username}, text contains @{bot_username}: {f'@{bot_username}' in text if bot_username else 'N/A'}")
    
    if not bot_username or f"@{bot_username}" not in text:
        # 没有 @ 机器人，不处理
        logger.info(f"Message does not mention bot, skipping immediate feedback")
        return
    
    # 立即发送确认消息（在主线程中）
    if photo_file_id:
        status_text = "📸 收到截图，正在分析图片和问题..."
    else:
        status_text = "📝 收到问题，正在分析..."
    
    logger.info(f"Sending immediate feedback to chat {chat_id}, reply_to {message_id}")
    status_result = send_message(chat_id, status_text, message_id, parse_mode=None)
    logger.info(f"Immediate feedback result: {status_result}")
    
    status_msg_id = None
    if status_result.get("ok"):
        status_msg_id = status_result.get("result", {}).get("message_id")
        logger.info(f"Immediate feedback sent successfully, status_msg_id: {status_msg_id}")
    else:
        logger.error(f"Failed to send immediate feedback: {status_result}")
        # 即使发送失败也继续处理，只是没有状态消息可更新
    
    # 在后台线程中处理消息
    thread = threading.Thread(
        target=process_message_async,
        args=(chat_id, message_id, user_id, text, photo_file_id, status_msg_id)
    )
    thread.daemon = True
    thread.start()


# ============================================================
# Webhook Routes
# ============================================================

@app.route('/', methods=['GET'])
def health_check():
    """健康检查端点"""
    return 'OK - Telegram Jira Agent Bot with Vision is running (Pure Webhook Mode v3)'


@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook 端点，接收 Telegram 更新"""
    try:
        update = request.get_json(force=True)
        update_id = update.get("update_id")
        
        logger.info(f"Received webhook update: {str(update)[:200]}...")
        sys.stdout.flush()
        
        # 消息去重检查
        if update_id and message_cache.is_duplicate(update_id):
            logger.info(f"Duplicate update_id: {update_id}, skipping...")
            return Response('OK', status=200)
        
        logger.info(f"Processing update_id: {update_id}")
        
        message = update.get("message")
        if not message:
            logger.info("No message in update, skipping...")
            return Response('OK', status=200)
        
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        user_id = message.get("from", {}).get("id")
        text = message.get("text") or message.get("caption") or ""
        has_photo = bool(message.get("photo"))
        
        logger.info(f"Message from chat {chat_id}, user {user_id}: text='{text[:50] if text else ''}...', has_photo={has_photo}")
        
        # 处理命令
        if text.startswith("/start"):
            handle_start_command(chat_id, message_id)
        elif text.startswith("/help"):
            handle_help_command(chat_id, message_id)
        elif text.startswith("/clear"):
            handle_clear_command(chat_id, message_id, user_id)
        else:
            # 处理普通消息
            photo_file_id = None
            if message.get("photo"):
                # 获取最大尺寸的图片
                photo_file_id = message["photo"][-1]["file_id"]
            
            handle_user_message(chat_id, message_id, user_id, text, photo_file_id)
        
        # 立即返回 200，让 Telegram 知道我们已收到消息
        return Response('OK', status=200)
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        import traceback
        traceback.print_exc()
        # 即使出错也返回 200，避免 Telegram 重试
        return Response('OK', status=200)


def setup_webhook():
    """设置 Telegram Webhook"""
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL not set, webhook will not be configured")
        return False
    
    webhook_url = f"{WEBHOOK_URL}/webhook"
    
    # 先删除旧的 webhook
    delete_result = telegram_api("deleteWebhook", {"drop_pending_updates": True})
    logger.info(f"Delete webhook response: {delete_result}")
    
    # 设置新的 webhook
    set_result = telegram_api("setWebhook", {
        "url": webhook_url,
        "drop_pending_updates": True,
        "allowed_updates": ["message", "edited_message"]
    })
    logger.info(f"Set webhook response: {set_result}")
    
    return set_result.get("ok", False)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    logger.info("Starting Jira Agent Bot in Pure Webhook Mode v4...")
    logger.info(f"WEBHOOK_URL: {WEBHOOK_URL}")
    logger.info(f"PORT: {PORT}")
    sys.stdout.flush()
    
    # 预热机器人用户名缓存
    bot_username = get_bot_username()
    logger.info(f"Bot username: {bot_username}")
    
    # 设置 Webhook
    if setup_webhook():
        logger.info("Webhook configured successfully")
    else:
        logger.warning("Webhook configuration failed or WEBHOOK_URL not set")
    
    # 启动 Flask 服务器
    logger.info(f"Starting Flask server on port {PORT}...")
    sys.stdout.flush()
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
