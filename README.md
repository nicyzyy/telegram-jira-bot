# Telegram-Jira BUG 自动化上报机器人

这是一个自动化的 BUG 上报系统，用户可以在 Telegram 群组中 @机器人 报告 BUG，机器人会自动分析 BUG 内容并在 Jira 中创建任务。

## 功能

- ✅ 监听 Telegram 群组中的消息
- ✅ 使用 OpenAI 自然语言分析 BUG 内容
- ✅ 自动识别 BUG 类型（开发 BUG 或 UI/设计 BUG）
- ✅ 自动在 Jira 中创建任务
- ✅ 根据 BUG 类型自动指派给对应负责人
- ✅ 发送 Telegram 确认消息

## 部署步骤

### 1. 创建 GitHub 仓库

```bash
# 在你的 GitHub 上创建一个新的仓库，然后：
git remote add origin https://github.com/YOUR_USERNAME/telegram-jira-bot.git
git add .
git commit -m "Initial commit"
git branch -M main
git push -u origin main
```

### 2. 在 Railway 上部署

1. 访问 [Railway.app](https://railway.app)
2. 点击 "New Project" → "Deploy from GitHub"
3. 选择你的 `telegram-jira-bot` 仓库
4. 配置环境变量（见下文）
5. 点击 "Deploy"

### 3. 配置环境变量

在 Railway 项目的 "Variables" 部分添加以下环境变量：

```
TELEGRAM_BOT_TOKEN=你的Telegram Bot Token
OPENAI_API_KEY=你的OpenAI API Key
JIRA_DOMAIN=huanbruan.atlassian.net
JIRA_EMAIL=zyy199057@gmail.com
JIRA_API_TOKEN=你的Jira API Token
```

## 使用方法

1. 在 Telegram 群组中 @机器人
2. 描述 BUG 内容
3. 机器人会自动分析并在 Jira 中创建任务
4. 你会收到一条确认消息，包含 Jira 链接

## 示例

```
@BugReporterBot 登录页面无法输入中文，输入框显示乱码
```

机器人会：
1. 分析这是一个 UI/设计 BUG
2. 在 Jira 中创建任务
3. 指派给设计负责人
4. 发送确认消息

## 技术栈

- Python 3.11+
- python-telegram-bot
- OpenAI API
- Jira Cloud API

## 故障排除

### 机器人没有响应

1. 检查 Railway 日志确认机器人是否在运行
2. 确认环境变量是否正确配置
3. 检查 Telegram Bot Token 是否有效

### Jira 创建任务失败

1. 检查 Jira API Token 是否过期
2. 确认 Jira 账户有权限访问该项目
3. 检查 IP 白名单设置

## 支持

如有问题，请查看日志或联系开发者。
