# QQQ LEAPS Alpha Bot (v1.0)

这是一个针对 Interactive Brokers (IBKR) 的全自动交易机器人，实施 **Core-Satellite (核心-卫星)** 策略，专注于 **QQQ LEAPS (长期期权)** 的“哑铃策略”。

## � Docker 部署 (Docker Deployment) - 推荐

### 1. 先决条件 (Prerequisites)
*   **Docker Desktop**: 确保已安装并启动。
*   **IB Gateway 容器**: 确保 `ibkr-gateway` 正在运行，并且加入到了 `qqq_default` 网络中。
    *   端口: 4004
    *   网络: `qqq_default`

### 2. 运行 (Running)
在项目根目录下执行以下命令：

```bash
docker-compose up --build -d
```
*   这会自动构建镜像并启动容器。
*   Bot 会自动连接到同一网络下的 `ibkr-gateway:4004`。

### 3. 查看日志 (Logs)
```bash
docker-compose logs -f
```

### 4. 停止 (Stop)
```bash
docker-compose down
```

## 🖥️ 本地部署 (Local Deployment)

1.  **先决条件**:
    *   已安装并运行 **Interactive Brokers Gateway** 或 **TWS**。
    *   **API 设置**: 在 TWS/Gateway 中启用 "Enable ActiveX and Socket Clients"。
    *   **端口**: 配置 TWS/Gateway 监听端口 **7497**。
    *   已安装 **Python 3.10+**。

2.  **安装依赖**:
    ```bash
    pip install -r requirements.txt
    ```

3.  **运行**:
    双击 `run.bat` 脚本，或在命令行执行：
    ```bash
    python main.py
    ```

## 🧠 策略逻辑 (Strategy Logic)

### 矛: 入场 (The Spear / Entry)
*   **标的**: QQQ 期权 (LEAPS)。
*   **触发条件**: 每 5 分钟检测一次，如果 QQQ 价格较 **前一日收盘价** 下跌幅度达到或超过 **-1%**。
*   **合约选择**:
    *   **到期日**: > 365 天 (一年以上)。
    *   **Delta**: ~0.6 (轻度实值 ITM)。
*   **过滤条件**: 
    *   **每日一单**: 严格限制每个自然日最多开仓 1 次。
    *   **最大持仓**: 最多同时持有 3 个合约。

### 盾: 出场/风控 (The Shield / Exit)
*   **阶梯止盈 (Stepped Take Profit)**: 根据持仓时间动态调整目标：
    *   **0-4 个月 (蜜月期)**: 目标 **+50%**。
    *   **4-6 个月**: 目标降至 **+30%**。
    *   **7-9 个月 (安全期)**: 目标降至 **+10%**。
*   **强制平仓 (Force Exit)**: 持仓超过 **270 天** (9个月) 且未达到止盈目标，强制卖出，防止时间价值加速损耗。

## 🛡️ 安全特性 (Safety Features)
*   **重启恢复 (Restart Resilience)**: 每次启动时，通过请求历史数据 (Historical Data) 获取权威的“昨日收盘价”，防止盘中重启导致数据偏差。
*   **价差保护 (Spread Protection)**: 如果 `(Ask - Bid) / Mid > 1%` (价差过大)，则拒绝交易，防止流动性不足导致的滑点。
*   **规模保护 (Size Protection)**: 单个合约权利金限制在 $12,000 以内。
*   **初始化重试 (Initialization Retry)**: 如果初始化失败 3 次，程序将暂停 15 分钟，防止触发 IBKR API 流量限制。

## 📂 文件结构 (File Structure)

*   `main.py`: 主程序入口，负责连接管理、重连循环和异常处理。
*   `strategy.py`: 核心策略逻辑 (信号扫描、LEAPS 筛选、出场检查)。
*   `execution.py`: 执行模块 (下单、中点价格计算、安全检查)。
*   `persistence.py`: 持久化层，管理 `trades.csv` 数据库，负责状态恢复。
*   `config.py`: 配置文件 (端口、参数限制、时区设置)。
*   `reporting.py`: 每日报告生成器。
*   `trades.csv`: 本地交易数据库 (程序运行时自动生成)。

## ⚠️ 免责声明 (Disclaimer)
本软件仅供教育和研究使用。自动化交易存在高风险，可能导致资金损失。在使用实盘资金前，请务必进行充分的模拟盘测试 (Paper Trading)。使用风险自负。
