# LongVol v0.7 策略、研究与部署手册

版本：应用与策略规则 `0.7.0`  
状态：默认 `SIMULATE`、`COLD_START_FIXED_RISK`、`option_mc_mode=RESEARCH_ONLY`  
适用范围：美股个股急跌后的短期错位反弹研究、长 Call 表达、Moomoo 执行与审计

> LongVol 的真实 mandate 是 **Short-Horizon Idiosyncratic Panic-Dislocation Rebound with Directional Convexity**。它预测的是数个交易日内的个股现货反弹；它不是长期价值反转，也不是 delta-neutral 的纯 long-vol 策略。期权是表达工具，不是 alpha 来源。

## 1. 本版结论与边界

### 1.1 已实施的结构修复

| 领域 | v0.7 规则 | 决策层级 |
|---|---|---|
| Panic 顺序 | 同一 shock bar 的异常成交量与价格/区间冲击；之后至少等待两个完整 session | hard gate |
| Compression | 只能在已确认 shock 之后成立；要求 shock 后成交量衰减，并有 ATR/RV 收缩 | hard gate |
| Reversal | 最新收盘改善、收在日内较高位置、没有再创新低 | hard gate |
| Fundamental | 必须明确为 `intact` 且事件属于流动性/技术错位；未知、结构性重估或证据不足不准入 | hard gate |
| Profile | 保留全部数据和几何指标，默认不再决定准入；只有质量合格且至少有 1 ATR 空间的日内 profile 才可设置目标 | diagnostic；可显式升格 |
| Short | weakening 只作诊断；新鲜且明显 deterioration 仍是 veto | modifier + veto |
| Gamma | unsigned `OI × gamma` 只标记集中区，默认不叫 dealer support、也不设置目标 | diagnostic |
| Market/sector residual | 原始值继续记录；缺少可靠 benchmark 时默认不阻断 | diagnostic；可显式升格 |
| IV percentile / surface | 阈值继续记录，默认不单独否决；报价质量与合约经济性仍然否决 | diagnostic；可显式升格 |
| 合约选择 | 先枚举所有满足执行约束的合约，再计算目标/止损与模拟结果；spread 只是约束和次级排序项 | hard execution/economics gate |
| 风险 | planned-stop risk 与 full-premium tail risk 分开计算、分开限额 | 双 hard cap |
| 退出 | 多张合约的 structure partial 只能发生一次；宽 spread 的风险止损升级为 `ALERT`，不再伪装 `HOLD` | hard state machine |
| 执行日 | 只允许信号后的下一个真实美股开市 session 入场 | hard execution gate |

### 1.2 尚未被证明的内容

本地审计时没有完成交易，因此当前无法从真实策略样本估计胜率、期望 R、偏度、CVaR 或 Kelly fraction。`40% rebound / 30% continuation`、状态波动率、IV 半衰期、spot–IV 相关性、delta/DTE 范围、2-session wait、ATR stop 和止盈参数全部仍是可审计的工程先验，不是已验证 alpha。

因此：

- Monte Carlo 的概率性输出默认只用于研究诊断；
- `VALIDATED_KELLY` 和 `option_mc_mode=OOS_GATE` 均保持关闭；
- 不因模拟结果漂亮就提高仓位；
- 不用今天的期权链回溯证明数日前发生过 option panic；
- 不把 LLM 自行找到或生成的 URL 当作 point-in-time 证据。

## 2. 策略预测期限与收益来源

经典 long-term reversal 使用多年形成与检验窗口；本策略只在最近数日识别 shock，等待数个 session，再研究约 3–10 个交易日的修复，最长持有只是风险兜底。两者不是同一种效应。短期反转文献把一部分回报解释为对流动性供给的补偿，高成交量下跌也可能包含暂时价格压力；但极端跌幅的纸面反转可能被 bid–ask bounce 与交易成本显著侵蚀。[^1][^2][^3][^4]

未对冲长 Call 的局部 P&L 可写为：

```text
dOption ≈ Delta × dS + 0.5 × Gamma × dS²
          + Vega × dIV + Theta × dt
          - spread - slippage - fees
```

主要盈利场景是 `dS > 0`，但策略同时预期 panic 后 `dIV < 0` 且时间流逝，所以正 delta/gamma 必须覆盖 IV crush、负 theta 和成交成本。即使标的上涨，long call 也可能因为 IV 下降而亏损；波动率风险溢价研究同样不支持把“买期权”本身当作正期望。[^5][^6][^7]

由规则推导出的策略收益不是正态或简单二项分布，而是至少包含：

| 隐状态 | 标的路径 | IV/流动性路径 | 期权结果倾向 |
|---|---|---|---|
| 临时错位、快速修复 | 数日快速触及目标 | IV 回落、spread 正常化 | delta/gamma 可能覆盖 crush，正收益 |
| 判断方向正确但太慢 | 小涨或横盘 | IV 回落、theta 累积 | 标的涨而期权仍可能亏 |
| False bottom | 小反弹后再破位 | IV 与 spread 再扩张 | planned stop 附近或更差 |
| 永久基本面重估 | 继续跌或 gap down | 报价可能失真、bid 消失 | 可能接近损失全部权利金 |
| 意外强反弹 | 快速 gap up | 任意 | 裸 Call 右尾，但会受结构/1.5R 退出截断 |

这也是 v0.7 不再用单一胜率和单一盈亏比描述真实分布的原因。

## 3. 全流程与决策责任

```text
Moomoo 粗筛与 tracked universe
        ↓
日线/日内/short/期权链与账户的时点快照
        ↓
同一 bar 的 panic price-flow 事件
        ↓
独立确认 + shock 后等待 + compression + reversal
        ↓
PIT 新闻/SEC evidence packet → DeepSeek 结构化事件分类
        ↓
entry-day surface 与全部可执行 Call 枚举
        ↓
概率无关的 target/stop 净经济性 gate
        ↓
条件 Monte Carlo 诊断（OOS 前不参与 gate）
        ↓
planned-stop risk + full-premium tail cap + portfolio cap
        ↓
下一个真实交易 session 的 fresh-quote revalidation
        ↓
成交回写、一次性 partial 状态、日线/日内退出与审计
```

确定性本地代码拥有 gate、合约选择、仓位和退出权。DeepSeek 只把已经封存的证据分类成结构化字段；新闻供应商只提供原始 evidence；Moomoo 只提供数据和执行。任何一个外部组件都不能单独产生交易。

## 4. Candidate、panic、compression 与 reversal

### 4.1 粗筛不是交易信号

默认 coarse screen 为美股、价格至少 USD 5、市值至少 USD 10 亿、5 日变化不高于 -8%、volume ratio 至少 2、20 日平均美元成交额至少 USD 2,000 万。它只追求高召回并进入 `tracked.csv`；精确 drawdown 和所有 signal 都由本地截止 `as_of` 的数据重算。

### 4.2 Panic 的必要条件

最近五根日 K 中，必须存在同一根 bar 同时满足：

- 成交量 z-score 至少 1.5；
- 且 return ≤ -5%、gap ≤ -2% 或 range/ATR ≥ 1.5 之一；
- 当前 60 日 drawdown ≤ -20%，5 日 drawdown ≤ -8%。

系统不再把一天的最大成交量与另一天的最大跌幅拼成不存在的 shock。Price-flow 仍需要独立 stress 证据；当前可来自 downside/Yang–Zhang，或来自与 shock 日期严格对齐的 option snapshot。

### 4.3 Shock-day 与 entry-day 不能混用

两个快照回答不同问题：

- `shock_day_option_snapshot`：当时是否有 IV jump、skew、term inversion 或 put flow stress；
- `entry_day_option_snapshot`：现在的期权是否足够正常、流动且值得按 ask 买入。

若 shock 已经是历史日期但本地没有当日链，`option_panic` 必须为 unknown/false，不能用当前链补确认旧事件。此时只有独立 downside/volatility 证据可以完成 panic confirm。每日同步会保留按日期分区的 option archive；这些文件只能证明从启用归档以后真实采集到的状态，不能重建更早的 NBBO 历史。

### 4.4 Compression 的严格顺序

`compression=true` 的顺序为：

1. `panic_confirmed=true`；
2. shock 后至少经过 `min_post_shock_sessions=2` 个完整 observation；
3. post-shock 平均成交量 / shock-day 成交量 ≤ 0.85；
4. ATR ratio 或 RV ratio 至少一个 ≤ 0.85；
5. 最新 close 高于前收、close location ≥ 0.65，且低点没有再破前两日低点。

因此 shock bar 本身的放量下跌不能叫 compression。两天与 0.85 仍需按 `shock+1/+2/+3/+4` cohort 做 walk-forward 验证。

### 4.5 弱 feature 的正确角色

- volume profile 回答“若反弹，目标与阻力在哪里”，不证明反弹会发生；
- short flow worsening 可以否决，但 weakening 暂不能证明卖方撤退；
- unsigned gamma 只能表示持仓集中价格带，无法知道 dealer 净方向；
- raw、market residual 与 sector residual 应全部留存，benchmark 缺失时不能偷偷用 0；
- IV percentile、skew 和 term structure 有研究价值，但可执行的目标/止损净 payoff 是更直接的 instrument gate。

所有上述字段都保留在 signal/SQLite 供消融研究；默认软化不等于删除。

## 5. Fundamental 与实时新闻证据链

### 5.1 Fundamental 是 regime classifier

短期 reversal 最重要的分层之一，是跌幅由临时价格压力还是现金流/估值信息驱动。实证研究发现，不能由 cash-flow news 解释的 loser return 更容易反转。[^3] Fundamental agent 因而不是“AI 加分项”，而是识别分布是否已从 temporary dislocation 切换到 permanent repricing。

事件类别互斥：

1. `LIQUIDITY_OR_TECHNICAL_DISLOCATION`；
2. `EARNINGS_OR_GUIDANCE_REPRICING`；
3. `BALANCE_SHEET_OR_FINANCING_BREAK`；
4. `ACCOUNTING_REGULATORY_LITIGATION`；
5. `M_AND_A_OR_CAPITAL_ACTION`；
6. `UNKNOWN`。

只有第一类在有精确来源、PIT collection 成功、`needs_review=false`、信心达标并明确 `thesis_status=intact` 时可以通过。结构性类别强制视为 valuation regime break；`UNKNOWN` 与没有新闻不能推导为 bullish。

### 5.2 四个时间戳

每条新闻/filing 至少保存：

- publisher `published_at`；
- provider `updated_at`；
- 本地首次收到 `first_seen_at` / `ingested_at`；
- 可用于模型的 `available_at`。

归档按 `provider + source_id + content_hash` append-only 保存修订。截止时间以后首次抓到的 REST 历史文章、截止后修订、PIT unknown 和 tombstone 都不能进入该日期的 evidence packet。`packet_sha256` 是内容校验和，不是数字签名。

### 5.3 当前 collector 架构

v0.7 提供统一 `NewsProvider` 接口及以下实现：

| Provider | 角色 | Gate eligibility |
|---|---|---|
| SEC EDGAR | 一手 filings 与部分 EX-99 附件 | 可识别结构性事件；不是 broad-news coverage |
| Direct Benzinga | 实时金融新闻、修订字段、WIIM 生态 | 仅在合同与 `BENZINGA_NEWS_REALTIME_ENTITLED=true` 后用于 gate |
| Alpaca News | Benzinga 派生的历史/实时新闻入口 | 仅在账户与 `ALPACA_NEWS_REALTIME_ENTITLED=true` 后用于 gate |
| Massive base news | 聚合参考新闻 | 官方基础 feed 为小时级更新，不作实时 entry gate |

SEC 的 submissions API 无需 API key，官方说明其发布后典型更新延迟少于一秒；自动访问仍须使用可识别的 User-Agent 并遵守不高于 10 requests/s 的 fair-access 要求。[^8][^9] Alpaca 官方接口每页最多 50 条，并明确无实时权限时默认数据至少延迟 15 分钟；其新闻当前来自 Benzinga。[^10][^11] Benzinga 提供实时 Newsfeed、removed/corrected news、WIIM 和 streaming 体系；生产使用前应书面确认算法内部使用、LLM 输入、摘要、历史保存与修订/删除处理权。[^12][^13] 两项 realtime entitlement 环境变量默认均为 `false`；仅持有 API key 不等于拥有实时资格。

实际落盘分为三类：`state/news/archive/{symbol}.jsonl` 是只追加的原始修订归档；`state/news/packets/{as_of}/{symbol}.json` 是从归档按 cutoff 重建并校验 hash 的模型 evidence；`research_packets/{symbol}.json|txt` 只是可选的本地 context，不是 provider evidence，不能自行取得 source allowlist 资格。文章若在历史 cutoff 后才被本机抓到，即使 publisher 的 `published_at` 更早，也只能进入 archive，不能进入该日期的 PIT gate。

### 5.4 DeepSeek 不能代替采集器

当前 DeepSeek Responses 接口不能替代采集器：本部署不执行 `web_search`，`web_search` 与 `mcp` 等不受支持的 built-in tool 可能被静默忽略。[^14] 显式 capability probe 虽可能返回成功响应，但当前实测没有 `web_search_call`；因此必须把 native search 视为 unavailable，并由外部 provider 采集后再把封存 packet 喂给模型。

即使以后 probe 成功，native search 也只允许作人工 discovery，不能通过 fundamental gate，因为它不能证明：

- 某条内容在历史 cutoff 前已经由本系统看到；
- 当时是哪一版文章；
- broad-news 覆盖是否完整；
- 内容是否允许发给第三方 LLM。

DeepSeek 只接收本地 evidence packet，输出中的 `sources` 与 `source_ids` 必须精确属于 packet allowlist，每条 evidence 必须引用其中的 ID。模型补写、修复或另找的 URL 都被过滤。DeepSeek 条款也要求调用方拥有处理输入所需权利；是否允许发送新闻全文取决于新闻合同，不取决于 API 能否读取。[^15][^16]

## 6. MCP、API 与推荐接入顺序

MCP 是 host/client/server 的 JSON-RPC 工具协议，不是新闻数据库，也不附带数据 entitlement 或再利用许可。Host 负责权限、编排与传给模型的上下文；server 只暴露 tools/resources/prompts。[^17] DeepSeek 当前不会原生执行 `mcp` tool；若采用 MCP，LongVol 必须自行充当 host，把 `tools/list` 的 schema 映射为 function tool，再把 `tools/call` 结果回填模型。

对每日交易流水，固定 REST/WebSocket collector 比让 LLM 自主调用 MCP 更合适，因为前者更容易做分页、重试、限速、时点封存、修订和供应商健康检查。MCP 可以作为研究终端的交互层，但不能绕过 archive。

推荐顺序：

1. SEC EDGAR 作为免费一手 filing 底座；它可发现结构性事件，但绝不等价于 broad-news coverage；
2. 确认账户 realtime entitlement 后，对 Alpaca News 做 30–60 天 shadow test，统计 50–100 个长尾 shock 的 coverage、首见延迟和修订；
3. 若漏报/延迟不可接受，采购 direct Benzinga News + WIIM + removed-news/streaming 权限，并在合同确认后才开启 realtime entitlement flag；
4. Massive base news 只用于小时级补充召回；若采购其 Benzinga add-on，再与 direct Benzinga 做价格、延迟和许可对照；
5. 如增加 Tiingo，利用 `crawlDate` 辅助 PIT 审计，但仍以本地 receive time 为最终边界。

本工作区当前没有已安装且适合该用途的金融新闻 connector/plugin。通用协作类 plugin 不能提供新闻 entitlement，因此 v0.7 直接实现 provider API 层，并保留未来 MCP adapter 的接口边界。

## 7. 条件 Monte Carlo：正确用途与数学边界

### 7.1 条件化思路

“股票参数作为 condition 去调整期权模拟分布”是正确分层：

```text
P(option P&L | point-in-time stock state, contract, execution rules)
```

上游股票过程给出当前 spot、ATR、target、hard stop、Yang–Zhang/ATR annualized volatility、持有期以及三种状态。期权层再模拟 spot path、IV mean reversion、spot–IV correlation、theta、target/stop first passage 和 ask→conservative bid。

反方向推断是不成立的：模拟不能从某张 Call 的高期望值证明股票反弹概率，也不能用被选择合约的回测结果重新调高同一次选择中的 rebound probability。概率必须来自冻结、严格 OOS 的股票事件模型。

### 7.2 三状态 mixture

当前透明先验为：

- `REBOUND`：terminal median 在结构目标附近并允许小幅 overshoot，IV 向 entry IV 的较低倍数收敛；
- `STABILIZATION`：spot 近似横盘、波动下降、IV 温和回落；
- `CONTINUATION`：terminal median 在 hard stop 以下，spot volatility 与 IV 上升。

每条路径按交易日推进，对每个合约使用相同随机数以降低排序噪声。期权 mark 由 Black–Scholes 产生，并用 entry mid 做模型尺度校准；入场始终用 ask，退出 mark 应用至少等于当前半价差的 haircut，stop 使用更保守 haircut。输出包括 expected/median return、P(profit)、目标/止损/时间退出概率、p05/p95、VaR/CVaR、按状态结果和平均退出日。

Black–Scholes 在跳跃、随机波动、离散股息和小盘流动性压力下并不是真实生成过程。这里使用它做路径内一致估值，不宣称它能复原真实 NBBO。模型误差必须由 OOS bid labels、haircut stress 和实盘 shadow fills 校准。

### 7.3 两种模式

`RESEARCH_ONLY` 是默认且唯一立即可用的模式：

- MC 概率、expected return、P(profit)、CVaR 和 robust score 全部只写入 diagnostics；
- hard economics 只使用不依赖状态概率的保守 target/stop scenario；
- target 必须在 ask、双边费用与 exit haircut 后有正净收益；
- stop scenario 必须为负净收益；
- 在通过者中按 deterministic target-payoff / stop-loss 排序，spread 只作次级 tie-breaker。

`OOS_GATE` 只有在配置同时提供非 `UNVALIDATED_PRIOR` 的来源和 `out_of_sample_validated=true` 时才允许加载；届时才可把 expected return、P(profit)、CVaR 与 robust score 作为 gate。配置加载器会 fail closed，不能仅把 mode 字符串改掉。

### 7.4 合约参数如何调整

Monte Carlo 不应直接优化一个样本内“最佳 delta/DTE”。正确做法是先定义少量经济上不同的 bucket，例如中 delta、高 delta、短中 DTE，再在完全相同的股票路径、ask/bid、费用和退出规则下比较。当前选择器已对全部通过执行约束的 Call 做 scenario/MC，而不是先按最窄 spread 选一张。

参数升级流程：

1. 固定 train window，仅用 train event 估计状态概率、状态 terminal return、vol multiplier、IV half-life/correlation 和 bid haircut；
2. 冻结参数 hash；
3. 在按时间向前的 validation/test window 比较 delta × DTE buckets；
4. 指标至少包含 after-cost median/mean R、5% CVaR、fill rate、target first passage、time-under-water 与 regime stability；
5. 只有多个 OOS fold 方向一致，才调整 contract filter 或启用 `OOS_GATE`；
6. 每次策略、数据源、fundamental schema 或 exit 语义变化都使旧证据失效。

### 7.5 为什么暂不自动切换到 bull call spread

若 target 明确且有限，debit spread 在经济上可能比裸 Call 更一致，因为 short leg 可抵消部分 vega、theta 和成本；代价是两腿 spread、成交概率、腿风险与美式提前指派。OCC 把 bull call spread 定义为有限风险、有限收益结构。[^18] v0.7 将其列入 OOS instrument tournament，但在没有组合 NBBO/执行数据前不自动上线。

## 8. 目标、止损、仓位与组合风险

### 8.1 Target

只有通过质量检查的日内 volume profile 且目标距 spot 至少 `min_room_to_resistance_atr` 才能设置结构目标。日线 fallback 仍被记录，但不能制造一个极近 target。Unsigned gamma 默认不能设置 target。没有可靠结构位时使用 `spot + 2 × ATR` 的透明 fallback。

### 8.2 三个不同概念

```text
thesis_stop_spot
    = 标的跌到该点时，短期反弹 thesis 失效

planned_option_exit_bid
    = 在 thesis stop + IV stress + time + haircut 下的保守期权退出价

full_premium_tail_risk
    = entry ask × multiplier + entry fee
```

`planned_stop_loss = (entry ask - planned exit bid) × multiplier + round-trip fees` 是 R 分母和 planned-risk cap；`full_premium_tail_risk` 是 gap、无 bid 或无法成交时的资本损失上限估计，并受独立 premium cap。两者不能取其一代替另一个。期权持有人可能损失全部权利金，stop 不是成交保证。[^19]

### 8.3 冷启动

```text
by_planned_risk = floor(equity × 2.5% / planned_stop_loss)
by_tail_cap     = floor(equity × 5% / full_premium_tail_risk)
contracts       = min(by_planned_risk, by_tail_cap, 1)
```

整个 cold-start portfolio 同样分别受 2.5% planned risk、5% full-premium tail risk、一个持仓、同一 underlying 一个持仓约束。USD 2,500 的 5% 权利金上限约 USD 125；这意味着大多数合约即使信号优秀也买不了一张，正确状态是 `WATCH`。

### 8.4 Kelly

当前 0 笔完成交易，不允许 Kelly。30 笔只能当工程 smoke test：真实胜率若约 50%，30 个独立 Bernoulli 样本的普通 95% 误差仍约 ±18 个百分点，而且市场/行业事件相关会降低有效样本量。

未来不应只用二元 `p,b`，而应从 OOS 多状态 R 分布求受约束增长率：

```text
f* = argmax_f Σ p_j log(1 + f × R_j)
```

再对参数不确定性、相关 exposure、CVaR 和 drawdown 收缩。任何 Kelly 上线仍需版本化 study、SHA-256、明确批准、非未来 sample end 和足够独立事件。

## 9. Entry 与 exit 状态机

### 9.1 Entry

收盘信号只在下一个真实 US market session 的 10:00–15:30 ET 有效。入场前重新验证：

- 市场日历与信号日期；
- option/underlying quote 时间与双边报价；
- live spread、ask drift、spot chase 与 hard stop；
- fundamental/news packet 是否被新 shock 或新材料事件失效；
- 当前 ask 下的 target/stop economics；
- 账户 USD 资金、planned risk、full-premium tail cap 与 portfolio capacity。

所有 entry 为 DAY limit。订单提交、修改和取消不盲目自动重试；网络超时不能证明 broker 没有接受第一次 mutation。真实下单仍需要 REAL 配置、环境密钥与逐次确认。

### 9.2 Exit 优先级

| 优先级 | 触发器 | 默认动作 |
|---:|---|---|
| 1 | `EXPIRY_RISK` | EXIT |
| 2 | `THESIS_BREAK` | EXIT |
| 3 | `UNDERLYING_HARD_STOP` | EXIT |
| 4 | `OPTION_PREMIUM_STOP_WIDE_SPREAD` | ALERT / 人工风险接管 |
| 5 | `OPTION_PREMIUM_STOP` | EXIT |
| 6 | `PROFIT_GIVEBACK` | EXIT |
| 7 | `TIME_STOP` | EXIT |
| 8 | `DTE_EXIT` | EXIT |
| 9 | `IV_CRUSH` | EXIT |
| 10 | `SHORT_PRESSURE_REACCELERATION` | EXIT |
| 11 | 首次 `STRUCTURE_TARGET_REACHED` | 多张 REDUCE；一张 EXIT |
| 12 | `OPTION_TARGET_REACHED` / 1.5R | EXIT runner |

多张 position 持久化 `partial_exit_taken`，已完成 structure partial 后不能在后续轮询重复减仓。宽 spread 的 premium stop 不再返回静默 HOLD；它产生 `risk_exit=true` 的 ALERT，提示使用可配置 marketable-limit/人工 broker 处理。硬风险不会因缺少 ancillary short/profile 数据而消失。

### 9.3 持有期解释

主 alpha 研究窗口是 3–10 个交易日，10-day MC horizon 是当前透明研究窗口。`max_holding_days=30` 只是兜底，不表示预期反转需要一个月。若真实成交频繁依赖 20–30 日 time stop，说明 strategy drift，应重新验证而非延长等待。

## 10. 安装与配置

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[openai,moomoo]'

cp .env.example .env
set -a; source .env; set +a
```

依赖固定为 `moomoo-api==10.10.7008` 与 `protobuf==5.29.5`；OpenD 也必须匹配项目要求。仅 socket connected 不代表 Screener、Option Rank 与 Trade API 可用。

DeepSeek + 新闻的最小 `.env`：

```bash
OPENAI_API_KEY="..."
OPENAI_BASE_URL="https://api.deepseek.com"
OPENAI_RESEARCH_MODEL="deepseek-flash"
LONGVOL_ALLOW_NATIVE_WEB_SEARCH=false

# 推荐先选一个 broad provider，再加 SEC
LONGVOL_NEWS_PROVIDERS="alpaca,sec"
APCA_API_KEY_ID="..."
APCA_API_SECRET_KEY="..."
ALPACA_NEWS_REALTIME_ENTITLED=false
BENZINGA_NEWS_REALTIME_ENTITLED=false
SEC_USER_AGENT="LongVol/0.7 contact@example.com"
```

生产 direct Benzinga 时改为 `LONGVOL_NEWS_PROVIDERS="benzinga,sec"` 与 `BENZINGA_API_KEY`。只有在供应商账户和合同均确认实时资格后，才把对应 `*_NEWS_REALTIME_ENTITLED` 改为 `true`。Massive 可用 `MASSIVE_API_KEY`，但基础新闻不具有实时 gate 资格。

不要把任何真实 key 放入 `.env.example`、文档或日志。`.env` 必须留在 `.gitignore`；已经暴露过的 key 应在供应商控制台撤销并重新签发。

## 11. 运行与数据位置

### 11.1 健康检查

```bash
longvol healthcheck --opend --broker --research \
  --config config/strategy.json \
  --sizing config/sizing.json \
  --trading-config config/trading.json
```

Healthcheck 只验证配置、凭据存在、OpenD/SDK/账户连接并报告新闻 provider/entitlement 配置，不调用付费 DeepSeek search。缺少合格 broad provider 不会让 healthcheck 整体失败；daily 会继续批次，但对应标的 fundamental gate fail closed。Native search 的显式 probe 是单独、可能计费的运维动作；即使 probe 成功也不会成为 trade evidence。

### 11.2 独立新闻同步与能力探测

```bash
# append-only archive + 截止 as-of 的 evidence packet
longvol news-sync --symbols TBBK,GPK \
  --start 2026-08-26 --as-of 2026-09-09 \
  --providers alpaca,sec --state-dir state \
  --local-packet-dir research_packets

# 必须显式确认；该请求可能计费，且输出只用于 capability discovery
longvol research-capabilities --billable-probe \
  --model deepseek-flash
```

`research_packets/{symbol}.json|txt` 只会作为不可信的可选 local context 合并；所有可引用 `source_ids` 和 URLs 仍必须来自带接收时间的 provider archive。历史 cutoff 后才抓到的内容会被保存供未来日期使用，但不会补写进过去的 PIT packet。

### 11.3 每日流程

```bash
python scripts/run_daily.py \
  --config config/strategy.json \
  --sizing config/sizing.json \
  --trading-config config/trading.json
```

同日流程应在美股收盘数据稳定后运行。历史 `as_of` 的期权和新闻都不能由当前状态伪造：期权只接受已有日期归档；REST 新闻在 cutoff 后才首次抓到时会标为非 PIT 并被排除。要支持可靠次日盘前补跑，必须在正常日持续落地 option/news archive，而不是出问题后才回填。

### 11.4 主要输出

| 路径 | 内容 |
|---|---|
| `data/candidate_inbox/{as_of}.csv` | 当日 Moomoo coarse screen |
| `data/tracked.csv` | 滚动研究 universe |
| `data/bars/`、`data/intraday/` | 日线与五分钟 K |
| `data/options/` | entry-day 最新链 |
| `data/options_history/{day}/` | 按日期保存的 option chain archive |
| `data/option_history.csv` | 本地逐日 near-ATM IV，不与 vendor percentile 混合 |
| `state/news/archive/{symbol}.jsonl` | append-only 新闻修订与首次接收时间 |
| `state/news/packets/{as_of}/{symbol}.json` | 截止 `as_of` 的可复验 evidence packet |
| `research_packets/{symbol}.{json,txt}` | 可选本地 context；不是 provider archive 或独立 gate evidence |
| `state/fundamentals/{symbol}.json` | DeepSeek 结构化分类与 packet hash |
| `state/signals/{as_of}.csv` | 每个 hard gate、diagnostic、合约排序与 MC 分布 |
| `state/exits/{as_of}.csv` | 每个 OPEN position 的退出状态 |
| `state/trading_log.sqlite3` | runs、features、gates、orders、fills、positions、exits、errors |

信号排查顺序：先看 `status/reasons`，再看 `recent_shock_day → panic_confirmed → sessions_since_recent_shock → compressed/reversal/not_new_low`，随后看 fundamental event/PIT 字段，最后看 `option_mc_contracts_*`、ranking summary、deterministic scenario、sizing 与 portfolio 两类风险。不要从 `passed_gates` 总数反推接近可交易；gate 没有可补偿权重。

## 12. OOS 研究与验收

### 12.1 两层 label

先证明 underlying alpha，再评价 instrument：

- underlying labels：shock 后 1/2/3/5/10/15/20 session 的 raw/residual return、MAE/MFE、target/stop first passage；
- instrument labels：当时 entry ask、每个 exit 的 bid、fees、spread stress、IV/Greeks、实际状态机 R；
- 同一 panic event 的连续日期不得冒充独立样本；
- 保存退市、并购、破产标的，避免 survivorship bias。

### 12.2 预注册实验

| 维度 | 最小对照 |
|---|---|
| entry delay | shock +1/+2/+3/+4 session |
| stabilization | 要求/不要求 compression |
| return | raw / market residual / sector residual |
| fundamental | liquidity / unknown / earnings-guidance / structural break |
| instrument | stock / high-delta Call / mid-delta Call / bull call spread |
| holding | 3/5/10/15/20 sessions 与 first-passage |
| weak features | profile/short/gamma/IV 各自单独 ablation |

使用 chronological walk-forward；对重叠持有期和同日/同行业事件做 purge、embargo 或 cluster bootstrap。报告完整 after-cost R distribution、confidence interval、CVaR、max drawdown、time-under-water、fill rate、spread sensitivity 和各 regime 稳定性，不只报告最佳 Sharpe。对尝试过的规则数量做 Deflated Sharpe / backtest-overfitting 调整。[^20][^21][^22]

### 12.3 上线顺序

1. `RESEARCH_ONLY + COLD_START_FIXED_RISK + SIMULATE`；
2. 30–60 日新闻 shadow coverage 与 PIT 审计；
3. 多个时间 fold 的 underlying edge 与 instrument tournament；
4. 模拟盘比较理论 bid、下单 limit 与真实 fill/slippage；
5. 固定风险小仓验收；
6. 独立批准 `OOS_GATE`；
7. 最后才研究 risk-constrained fractional Kelly。

## 13. 已知限制与下一步

- 当前 MC 是三状态、Black–Scholes mark 与简化 IV process；没有离散跳跃、随机利率/股息、完整 order-book fill model 或多标的相关 shock。
- 当前 instrument production path 仍是单腿 Call；股票与 debit spread 只属于研究 tournament。
- SEC 不是 broad news；issuer IR/RSS、exchange halt、Benzinga removed-news 与持续 WebSocket receive-time collector 尚需补齐。
- REST backfill 无法证明历史可得性。严格 fail-closed 会牺牲一部分历史覆盖，这是防止 look-ahead 的必要代价。
- Option Screening union 不是完整 constant-maturity surface；未来应记录 expiry/right/delta bucket coverage，不足时输出 `UNKNOWN`。
- 组合目前主要控制持仓数、planned risk 与 premium tail risk；进一步上线 Kelly 前，还需聚合 delta dollars、vega/gamma/theta、sector beta、earnings clustering 与 liquidity stress。
- `packet_sha256` 只能检测内容变化；若需要不可抵赖审计，应增加签名、只追加对象存储与 retention policy。

## 参考资料

[^1]: Stefan Nagel, “[Evaporating Liquidity](https://www.nber.org/papers/w17653),” *Review of Financial Studies*, 2012.
[^2]: John Y. Campbell, Sanford J. Grossman, and Jiang Wang, “[Trading Volume and Serial Correlation in Stock Returns](https://www.nber.org/papers/w4193),” *QJE*, 1993.
[^3]: Zhi Da, Qianqiu Liu, and Ernst Schaumburg, “[A Closer Look at the Short-Term Return Reversal](https://doi.org/10.1287/mnsc.2013.1766),” *Management Science*, 2014.
[^4]: Don R. Cox and David R. Peterson, “[Stock Returns following Large One-Day Declines](https://doi.org/10.1111/j.1540-6261.1994.tb04428.x),” *Journal of Finance*, 1994.
[^5]: Options Industry Council, “[Long Call](https://www.optionseducation.org/strategies/all-strategies/long-call).”
[^6]: Gurdip Bakshi and Nikunj Kapadia, “[Delta-Hedged Gains and the Negative Market Volatility Risk Premium](https://doi.org/10.1093/rfs/hhg002),” *RFS*, 2003.
[^7]: Amit Goyal and Alessio Saretto, “[Cross-section of Option Returns and Volatility](https://doi.org/10.1016/j.jfineco.2009.01.001),” *JFE*, 2009.
[^8]: U.S. SEC, “[EDGAR Application Programming Interfaces](https://www.sec.gov/search-filings/edgar-application-programming-interfaces),” updated April 8, 2025.
[^9]: U.S. SEC, “[SEC to Apply New Rate Control Limits to EDGAR Websites](https://www.sec.gov/filergroup/announcements-old/new-rate-control-limits),” last reviewed June 28, 2024.
[^10]: Alpaca, “[News Articles API](https://docs.alpaca.markets/us/reference/news-3),” current API reference.
[^11]: Alpaca, “[Historical News Data](https://docs.alpaca.markets/us/docs/historical-news-data),” current documentation.
[^12]: Benzinga, “[Newsfeed API Overview](https://docs.benzinga.com/api-reference/news-api/overview),” current documentation.
[^13]: Benzinga, “[Stock News API](https://www.benzinga.com/apis/en-ca/cloud-product/stock-news-api/),” current product documentation.
[^14]: DeepSeek, “[Using the Responses API](https://api-docs.deepseek.com/guides/responses_api/),” current compatibility documentation.
[^15]: DeepSeek, “[Open Platform Terms of Service](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html),” current terms.
[^16]: DeepSeek, “[Terms of Use](https://cdn.deepseek.com/policies/en-US/deepseek-terms-of-use.html),” updated March 27, 2026.
[^17]: Model Context Protocol, “[Architecture](https://modelcontextprotocol.io/specification/2025-06-18/architecture),” protocol revision 2025-06-18.
[^18]: OCC, “[Options Strategies Quick Guide](https://www.theocc.com/getcontentasset/f34f8a0d-806f-4f1a-adf7-d49d8d94b16e/dfc3d011-8f63-43f6-9ed8-4b444333a1d0/option-strategies-quick-guide.pdf).”
[^19]: OCC, “[Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document),” June 2024 edition.
[^20]: David H. Bailey and Marcos López de Prado, “[The Deflated Sharpe Ratio](https://doi.org/10.2139/ssrn.2460551),” *Journal of Portfolio Management*, 2014.
[^21]: Campbell R. Harvey, Yan Liu, and Heqing Zhu, “[… and the Cross-Section of Expected Returns](https://www.nber.org/papers/w20592),” *RFS*, 2016.
[^22]: David H. Bailey et al., “[The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf),” *Journal of Computational Finance*, 2017.

本手册的证据审计与设计理由见 [`strategy_evidence_audit.md`](strategy_evidence_audit.md)。
