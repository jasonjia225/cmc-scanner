# CMC 10标的自动扫描器（初版）

这是公开行情**研究工具**，不会下单。只有真实获取的完整OHLC才会标记为成功。当前版本**不会自动生成可执行交易信号**，因为无法自动核实CMC实时买卖价、点差、经济事件和完整的至少2:1盈亏比。

## 部署
1. 在GitHub创建一个新仓库（建议public；行情报告会公开），上传本文件夹所有文件，包括 `.github/workflows/scan.yml`。
2. 仓库 Settings → Actions → General → Workflow permissions 选择 **Read and write permissions**，保存。
3. Actions → **CMC 10-asset hourly scan** → **Run workflow** 手动测试。
4. 运行成功后，打开仓库 `reports/latest.md` 和 `reports/latest.json`。定时运行使用UTC每小时34分（新西兰夏令时也是每小时34分）。GitHub定时任务可能延迟或跳过；需监控Actions运行记录。
5. 公开报告原始链接：`https://raw.githubusercontent.com/你的用户名/你的仓库名/main/reports/latest.md`。请先确认仓库默认分支是main。

## 与ChatGPT连接的实际限制
GitHub Actions**不能直接发送消息到现有ChatGPT聊天**。可把上述公开报告URL写进已有ChatGPT定时任务提示词，要求每小时读取、检查scan_utc是否为最新，并转述结果；但ChatGPT自动任务能否稳定访问raw URL必须实际测试，**不能承诺可靠推送**。这属于实验性中转，而不是已实现的ChatGPT推送API。

## 数据映射
- BTC/ETH: Coinbase Exchange公开现货K线（15m/1h/1d）；与CMC报价有差异。
- NDX、HSI、USDJPY、NZDUSD、黄金GC=F、白银SI=F、WTI CL=F: Yahoo Finance图表数据。黄金、白银、WTI使用**期货代理**，与CMC Cash CFD有基差，尤其注意期货换月。
- 4h: 从四根连续的1h K线按UTC完整聚合，缺失时不生成。
- China A50: 必须核实CMC可交易月份和到期日后才能接入；当前不发信号。

## 已知限制及下一步
- Yahoo、Coinbase可能限流/屏蔽GitHub机房IP；程序逐周期记录HTTP或网络错误，不会伪造成功。
- 指数休市、不同交易所时区、Yahoo期货换月、行情延迟需单独完善；4h目前采用UTC边界。
- 部分品种没有稳定免费分钟级OHLC；必要时接入授权行情服务。
- 当前指标使用至少60根完整K线计算EMA20/50、RSI14、ATR14、20根支撑阻力。严格排除未收盘K线。
- 当前仅有初步观察候选，不自动发布入场价/止盈止损；必须后续增加事件、点差和严格2:1风险回报校验。
- 每小时报告包含最新完整K线收盘时间、数据源和失败原因。可自行更换报价供应商。

## 本地运行
```
pip install -r requirements.txt
python scanner.py
```
