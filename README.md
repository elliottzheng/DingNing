<div align="center">

# 叮咛 · DingNing

**教师的智慧日程 —— 会听懂自然语言的课表与事务助手**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.x-00948B)](https://fastapi.tiangolo.com/)

*A natural-language timetable & task assistant for teachers. Built with FastAPI + SQLite, no frontend framework required.*

</div>

---

## 这是什么

一个为一线教师打造的**单机多用户教学工作台**：周期课表、临时调课、待办、按条目提醒、微信/APP 推送，全部可以用**一句自然语言**完成。

> 「明晚8点半帮我加个日程，提前20分钟提醒我」
> 「把周二的课调到周四下午1」
> 「11月4-5号校运会，停课」
> ——说人话就行，系统理解、执行、可撤销。

设计上它只为一种人服务：**时间被课表、调课、临时事务撕碎的班主任和科任老师**。

## 特性

### 课表与调课
- **周期课表**：支持「年级大课表」和「个人课表」两种 Excel 格式，明文文本导入，多次导入自动版本化
- **调课模型**：调课 = 原课停用 + 单次课行，一一对应、随时可查，不产生语义混乱的"日程"
- **科目别名**：格子里的「文」「语」自动归一到「语文」，教多门课各归各组
- **调休日**：周日按周二上课？一行配置，全系统（视图/推送/提醒/归属）自动跟随

### 广义日程提醒
- 日程、待办、课都可单独设置「提前 N 分钟提醒」（准点/5/15/30 分钟/1 小时/1 天）
- 后台每分钟扫描，到点推送；重启不重复、漏发自动补
- 占用判断：被调走的课如果原时段已被占据则不再显示，空闲则灰色提示

### 推送
- **WxPusher APP 推送**（推荐）：免费、无条数限制、真系统通知；扫码订阅、UID 自动回填
- **PushPlus 微信推送**：兜底渠道，微信内直接接收
- 每晚定时「明日安排」摘要；日报式课堂记录（可选）

### 自然语言与智能体
- 单条指令即可完成多步操作（调课 = 停课 + 补课两步，原子化执行，可一键撤销）
- 语义搜索：事件按语义相近度检索（BAAI/bge-large-zh 向量）
- 意图理解、时刻换算（「明晚8点半」→ 明天 20:30）内置提示词工程
- AI 不可用时全链路降级：规则解析兜底，**永远可用**

### 多用户
- 一台机器、多位老师各自账号独立数据库，互不可见
- 邀请码注册、密码 PBKDF2 哈希、登录限流、会话管理

## 快速开始

```bash
git clone https://github.com/elliottzheng/DingNing.git
cd dingning
pip install -r requirements.txt
uvicorn server:app --host 127.0.0.1 --port 8321
```

浏览器打开 `http://127.0.0.1:8321`，注册首个账号（自动成为管理员）。

- 首个账号即管理员，后续教师通过设置页的邀请码注册
- 课表导入：设置 → 课表导入 → 粘贴明文课表或上传 Excel
- 推送（可选）：设置 → 推送 → 填 WxPusher UID 或 PushPlus Token

详见 [docs/部署指南.md](docs/部署指南.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/部署指南.md](docs/部署指南.md) | 部署、开机自启、局域网/公网访问、多用户管理 |
| [docs/开发指南.md](docs/开发指南.md) | 代码地图、加功能流程、测试、设计系统 |
| [docs/数据模型.md](docs/数据模型.md) | 全部数据表与配置键口径 |
| [docs/设计系统.md](docs/设计系统.md) | UI 设计令牌与组件规范 |
| [docs/FAQ.md](docs/FAQ.md) | 常见问题 |

## 技术栈

- **后端**：Python · FastAPI · SQLite（单文件，零运维）
- **前端**：原生 ES Modules + CSS，无框架无构建
- **推送**：WxPusher（APP）/ PushPlus（微信）
- **AI（全部可选，不可用自动降级）**：DeepSeek（自然语言理解）、SiliconFlow BAAI 向量（语义搜索）

## 设计原则

1. **单文件 SQLite**：备份 = 复制一个文件
2. **无框架前端**：浏览器直接跑，改版即刷新
3. **AI 是加速器不是依赖**：任何 AI 服务挂掉，系统照常工作
4. **隐私优先**：所有数据本机存储；学生/个人数据不进任何第三方

## 适用场景

- 班主任/科任老师的个人课表与事务管理
- 学校教研组内部分署（多教师各管各的课表）
- 任何「周期 + 临时调整」混合型日程的个人管理

## License

[MIT](LICENSE)
