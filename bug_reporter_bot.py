import os
import json
import requests
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Environment Variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = "BB"
PORT = int(os.getenv("PORT", 10000))

# --- Assignee IDs ---
ASSIGNEE_DEV = "712020:29364cb3-1ba1-453c-8e28-4e0306787939"
ASSIGNEE_UI = "712020:7b0eae8d-9cc3-406b-814e-bbbe51c67cbd"

# --- Health Check HTTP Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK - Telegram Jira Bot is running')
    
    def log_message(self, format, *args):
        pass  # Suppress logging

def start_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    print(f"Health check server running on port {PORT}")
    server.serve_forever()

# --- OpenAI Function ---
def analyze_bug_report(text):
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = f"""你是一个BUG分析助手。请分析以下用户报告的BUG信息，并以JSON格式返回结果。

JSON格式要求：
{{
  "bug_title": "BUG标题",
  "bug_description": "BUG详细描述",
  "bug_type": "开发BUG 或 UI/设计BUG"
}}

用户报告：
{text}"""
    data = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }
    response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]

# --- Jira Function ---
def create_jira_issue(bug_data):
    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue"
    auth = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    assignee_id = ASSIGNEE_DEV if "开发" in bug_data.get("bug_type", "") else ASSIGNEE_UI

    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": bug_data.get("bug_title"),
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": bug_data.get("bug_description")
                            }
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
    return response.json()

# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("你好！我是BUG上报机器人。请在群里@我并描述BUG详情，我会自动分析并提交到Jira。")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # 获取机器人用户名
    bot_username = (await context.bot.get_me()).username
    
    # 只在被@时触发（群组和私聊都需要@机器人）
    is_mentioned = f"@{bot_username}" in update.message.text
    
    if not is_mentioned:
        return

    user_report = update.message.text.replace(f"@{bot_username}", "").strip()
    
    if not user_report:
        await update.message.reply_text("请描述你遇到的BUG问题。")
        return
    
    await update.message.reply_text("正在分析BUG，请稍候...")

    try:
        # 1. Analyze with OpenAI
        analysis_result = analyze_bug_report(user_report)
        bug_data = json.loads(analysis_result)

        # 2. Create Jira Issue
        jira_response = create_jira_issue(bug_data)
        issue_key = jira_response["key"]
        issue_url = f"https://{JIRA_DOMAIN}/browse/{issue_key}"

        # 3. Send Confirmation
        confirmation_message = (
            f"✅ BUG已成功提交到Jira！\n\n"
            f"📋 BUG信息：\n"
            f"标题：{bug_data.get('bug_title')}\n"
            f"类型：{bug_data.get('bug_type')}\n\n"
            f"🔗 Jira链接：{issue_url}"
        )
        await update.message.reply_text(confirmation_message)

    except Exception as e:
        error_message = str(e)
        print(f"Error: {error_message}")
        await update.message.reply_text(f"处理失败，发生错误：\n{error_message}")

# --- Main Function ---
def main():
    # Start health check server in a separate thread
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()
    
    # Delete any existing webhook first to avoid conflicts
    print("Deleting any existing webhook...")
    delete_webhook_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
    try:
        response = requests.get(delete_webhook_url)
        print(f"Webhook deleted: {response.json()}")
    except Exception as e:
        print(f"Warning: Could not delete webhook: {e}")
    
    # Start Telegram bot with drop_pending_updates to avoid conflicts
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Telegram Bot is running...")
    # Use drop_pending_updates=True to avoid processing old messages and conflicts
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
