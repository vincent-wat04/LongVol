# 输入文件

- `candidates.csv`: 每日收盘后由 Moomoo Stock Screening V2 自动生成的候选股，至少包含 `symbol` 和 `as_of`。
- `bars/<SYMBOL>.csv`: 日线 OHLCV，按日期升序。
- `intraday/<SYMBOL>.csv`: panic leg 的 5 分钟 OHLCV，用于 volume profile；`sync` 保存精确 `time`，生产买入门槛默认必须存在。
- `options/<SYMBOL>.csv`: Option Screener 的 OI/成交量并集，并用 market snapshot 刷新可交易 call。`iv`、`delta`、`gamma`、`open_interest`、`multiplier` 和 as-of 当天 `quote_time` 是关键字段。
- `option_regime.csv`: Moomoo `Option Underlying Rank` 的逐日真实快照，包含 IV、IV rank 与 IV percentile。仅供冷启动 IV gate 使用，不回填或污染本地 IV 序列。
- `short.csv`: 分开记录低频 short interest 与 daily short-volume flow；后者不是持仓量。
- `option_history.csv`: 每日由本地期权面提取的 near-ATM IV，始终独立积累。少于 60 个先验观测时不能启用 `VALIDATED_KELLY`，但可由同日 Moomoo IV percentile 通过冷启动 IV gate。
- `fundamentals.csv`: OpenAI 研究结果及来源、日期、置信度和人工复核标记。
- `positions.csv`: 已实际建仓的期权仓位，交付时只有表头；用于 exit 建议。开仓时必须冻结 stop、target、初始风险与 contract multiplier，平仓后保留 `CLOSED` 行作审计。
- `order_intent_example.json`: 单笔冷启动限价订单模板；正式入场应优先由 `intraday-monitor` 从 `PILOT_CANDIDATE` 或 `BUY_CANDIDATE` 自动生成，模板仅用于 dry-run/故障排查。
- `trade_open_example.json`: 非 Moomoo 自动成交的人工恢复模板；Moomoo 已知终态成交默认由订单对账自动写入。
缺失 gamma、OI、short、5 分钟 profile 或可靠基本面时，系统不会用虚构值补齐。缺本地 IV 历史时只允许使用同一交易日的 Moomoo vendor IV percentile；若该快照也缺失或陈旧，则进入 `WATCH`。Kelly 研究证据不足时采用受限固定风险，而不是默认胜率。
