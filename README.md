# QQQ LEAPS Mastery Bot (v2.1)

这是一个针对 Interactive Brokers (IBKR) 的高级全自动交易系统，实施 **Core-Satellite (核心-卫星)** 策略，专注于 **QQQ LEAPS (长期期权)** 交易。

v2.0 版本引入了 **Web 控制面板**、**SQLite 数据库** 和 **动态配置系统**，使管理更加安全和灵活。

## ✨ 新特性 (v2.0 Features)

*   **📊 Web 仪表盘**: 实时监控连接状态、持仓详情、账户资产（净值/现金/购买力）和策略配置。
*   **🏦 综合持仓查看**: 自动汇总并分类显示账户内的股票、期权、债券以及现金余额。
*   **🔄 自动 ROLL 规则**: 满仓大跌时自动“卖旧换新”，下移执行价并重置到期时间。
*   **📈 利润循环 (Profit Recycling)**: 自动将期权已实现利润投向 QQQM 现货，实现不动用本金的自动化复利。
*   **⚙️ 动态配置**: 网页端实时调整 Target Delta, Entry Drop %, ROLL 阈值及定投参数。
*   **🔒 安全认证**: 首次启动强制初始化管理员账号，支持密码登录和会话管理。
*   **💾 SQLite 持久化**: 替代 CSV，提供更可靠的数据存储和并发支持。

---

## 🚀 部署指南 (Deployment)

### 1. Docker 部署 (推荐)

**先决条件**:
*   已运行 `ibkr-gateway` 容器 (Port 4004, Network `qqq_default`)。
*   确保 VPS 防火墙放行 **8000** 端口。

**启动步骤**:

1.  **准备数据卷** (用于持久化保存账号和配置):
    ```bash
    docker volume create qqq-bot
    ```

2.  **拉取并运行**:
    ```bash
    # 确保 docker-compose.yml 存在
    docker-compose pull
    docker-compose up -d
    ```

3.  **初始化系统**:
    *   打开浏览器访问: `http://<YOUR_VPS_IP>:8000`
    *   按照向导创建管理员 (Admin) 账号。
    *   登录后即可进入 Dashboard。

### 2. 查看日志

```bash
docker-compose logs -f
```

---

## 🧠 策略逻辑 (Strategy Logic)

本系统由两个并发进程驱动：**交易引擎 (Strategy Loop)** 和 **Web 服务器 (FastAPI)**。

### 矛: 入场 (The Spear / Entry)
*   **标的**: QQQ 期权 (LEAPS)。
*   **触发条件 (可配置)**: 默认每 5 分钟检测一次，当 QQQ 较昨日收盘价下跌 **-1%** (Entry Drop Pct) 时触发。
*   **合约选择**:
    *   **到期日**: > 365 天 (默认)。
    *   **Delta**: ~0.6 (默认，可配置)。

### 盾: 出场/风控 (The Shield / Exit)
*   **阶梯止盈 (Stepped Take Profit)**:
    *   **0-4 个月**: 目标 +50%。
    *   **4-6 个月**: 目标降至 +30%。
    *   **> 6 个月**: 目标降至 +10%。
*   **强制平仓 (Force Exit)**: 持仓超过 **270 天** (默认) 强制离场，规避时间价值损耗。

### 🔄 滚动: ROLL 规则 (The Roll / Rebalance)
*   **目的**: 在大跌行情中优化成本，重置杠杆。
*   **触发条件**: 
    1.  持仓数量已达到 **最大限制 (Max Positions)**。
    2.  当日跌幅达到 **ROLL 触发跌幅** (默认 -5%)。
*   **执行逻辑**: 自动识别并卖出最早买入的 (FIFO) LEAP 合约，并立即按最新价格买入一个符合过滤条件的新合约（通常具有更低的执行价）。

### 📈 利润循环: 自动定投 QQQM (Profit Recycling)
*   **核心逻辑**: 将期权策略作为“现金流发动机”，将 QQQM 现货作为“财富储蓄罐”。
*   **会计准则**: 机器人精确追踪 `累计期权净利润 - 已投现货资金`。
*   **触发条件**: 
    1.  累计净利润池（扣除手续费后）足以购买 QQQM。
    2.  余额超过用户设定的 **“起投阈值”**（默认 $500）。
*   **执行方式**: 自动按市价/中价买入 QQQM 股数，并在数据库中记录已部署的利润金额，确保只动用赚来的钱。

---

## 📂 文件结构 (File Structure)

*   **`main.py`**: 系统入口，同时启动 `ib_insync` 循环和 `FastAPI` 服务器。
*   **`strategy.py`**: 核心交易逻辑，现在从 DB 读取配置。
*   **`persistence.py`**: SQLite 数据库层 (处理 `admin_users`, `system_settings`, `trades`)。
*   **`web/`**: Web 模块。
    *   `server.py`: FastAPI 应用与 API 路由。
    *   `auth.py`: 密码哈希与 JWT 认证。
    *   `templates/`: Jinja2 前端模板 (`dashboard.html` 等)。
*   **`Dockerfile`**: 包含 Python 3.10 环境及 Web 依赖。

---

## ⚠️ 免责声明 (Disclaimer)
本软件仅供教育和研究使用。自动化交易存在高风险，可能导致资金损失。在使用实盘资金前，请务必进行充分的模拟盘测试 (Paper Trading)。使用风险自负。
