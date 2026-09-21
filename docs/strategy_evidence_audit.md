# LongVol 策略证据与一致性审计

## 执行摘要

LongVol 当前捕捉的不是传统意义上的 long-term reversal，而是**个股在急跌与异常流量冲击后的短期、路径依赖型反弹**。其有效持有期应以数个交易日到约两周为主要研究区间；配置中的 15 日目标情景可以作为上界场景，但 30 日最长持有期更适合被解释为风险兜底，而不是 alpha 的预期兑现时间。De Bondt–Thaler 的 long-term reversal 使用 36 个月形成期和 36 个月检验期；当前系统则在最近 5 根日 K 中找 shock、至少等待 2 个完整交易日、次一交易时段执行，并以 15 日情景和 30 日强制退出管理仓位，两者不是同一种效应。[^1]

策略最合理的经济表述是：

> **在排除基本面永久性重估后，为短期个股流动性错位提供资本，并用有限损失的方向性凸性工具表达反弹。**

这一定义比“long volatility”更准确。当前持有的是未对冲长 Call，核心收益来源是正 delta 与正 gamma；而目标情景明确假设 IV 下跌 15%，同时承受负 theta。换言之，它是在**付费购买 vega，但在主要盈利场景里预期 vega 亏损**。方向判断必须足够快、足够大，才能覆盖 IV crush、时间价值与双边价差。OCC 也明确指出，标的上涨时 long call 仍可能因波动率下降而贬值。[^2]

当前最值得保留的设计是：顺序 gate、fail-closed、shock 后再 compression、用 ask 入场/bid 退出、冻结入场风险字段、冷启动禁用 Kelly、以及严格的时间戳审计。最需要优化的不是 panic 阈值本身，而是以下四个跨组件冲突：

1. **alpha 与工具不一致**：标的短期反弹是核心假设，裸长 0.25–0.45 delta Call 却同时暴露于预期中的 IV crush。
2. **目标、止损与仓位不一致**：结构目标和 1.5R 会截断右尾；实际损失又可能因 gap、bid 消失或 stop 定义而超过建仓时的 1R。
3. **事件时间与观测时间不一致**：panic 事件可能发生在数日前，但 option panic 与入场 surface 都用当前链面计算；“当时恐慌”和“现在已正常化”尚未被分成两个快照。
4. **统计模型与退出分布不一致**：Kelly 使用二元胜/负模型，实际退出包含时间损耗、IV crush、结构目标、gap、thesis break、分批退出等多状态路径。

截至当前本地审计库有 26 次 run、80 个 candidate snapshot、32 个 feature snapshot，但 **0 笔成交、0 个退出决定、0 个 broker order**；32 个评价中 28 个为 `NO_TRADE`、4 个为重复运行产生的 `WATCH`。因此项目目前没有可用于估计胜率、期望 R、偏度、尾部损失或 Kelly fraction 的真实策略收益分布。下文对收益分布的描述是由规则推导出的**结构性分布假设**，不是实证结果。

本地实现判断以 [`strategy.py`](../src/longvol/strategy.py)、[`metrics.py`](../src/longvol/metrics.py)、[`pricing.py`](../src/longvol/pricing.py)、[`kelly.py`](../src/longvol/kelly.py)、[`exits.py`](../src/longvol/exits.py)、[`intraday.py`](../src/longvol/intraday.py)、[`portfolio.py`](../src/longvol/portfolio.py) 及三份当前 JSON 配置为准；运行统计来自 `state/trading_log.sqlite3`，快照日为 2026-09-09。文档与代码冲突时，本报告按可执行代码解释。

## 1. 当前策略究竟在交易什么

### 1.1 代码中的时间轴

| 阶段 | 当前规则 | 隐含时间尺度 |
|---|---|---|
| 粗筛 | 5 日跌幅 ≤ -8%，60 日回撤 ≤ -20% | 急性个股下跌 |
| shock | 最近 5 根日 K 内，同一根 K 上出现异常量与价格/gap/range 冲击 | 事件日附近 |
| panic confirm | price-flow 必须成立，并由 downside/Yang–Zhang 或 option stress 二次确认 | 日/周级压力 |
| stabilization | shock 后至少 2 个完整交易日，量能衰减，ATR/RV 至少一项收缩，再要求 reversal/no-new-low | 约 2–4 个交易日 |
| execution | 收盘信号后，在 10:00–15:30 ET 执行；当前用 1–4 个日历日表示信号年龄 | 下一交易时段附近 |
| target scenario | 标的到结构目标、IV 下跌 15%，15 个日历日后重估期权 | 约 10–11 个普通交易日 |
| exits | 结构/期权目标、1.5R、IV crush、止损、DTE、30 日 time stop | 数日到一个月 |

这条时间轴和短期 reversal 文献一致得多。Nagel 将短期反转收益解释为流动性供给回报，而且该回报在 VIX 高、市场中介资本紧张时更高。[^3] Campbell、Grossman 与 Wang 的模型和实证指出，高成交量下跌比低成交量下跌更可能伴随未来预期收益上升。[^4] Da、Liu 与 Schaumburg 进一步发现，不能由现金流新闻解释的短期跌幅更容易反转；在 loser 一侧，主要机制是 fire-sale/liquidity shock。[^5]

但这些证据不证明任意急跌都会回归旧价格，也不直接证明当前 `-20%/-8%/z=1.5/2 天/0.85` 是最优参数。Cox 与 Peterson 发现，极端单日下跌后的短期反转很大一部分可由 bid–ask bounce 和流动性解释，同时这些股票在更长区间内表现仍可能较差。[^6] Avramov、Chordia 与 Goyal 也发现短期反转最强的往往是高换手、低流动性股票，但纸面利润可能小于交易成本。[^7]

### 1.2 为什么不是 long-term reversal

长期反转研究通常用年作为形成与持有尺度。De Bondt–Thaler 的经典结果比较过去 36 个月 winner/loser，并观察之后 36 个月，甚至讨论 5 年效应。[^1] 另一方面，3–12 个月区间存在著名的中期 momentum：过去 3–12 个月的赢家/输家倾向在随后 3–12 个月延续。[^8]

因此，LongVol 不应把“跌得深”解释成长期均值回归。它只应押注一个短期流动性修复窗口，并在窗口消失时离场。若仓位常被持有到 20–30 日，策略已经从事件反弹逐渐漂移到没有明确证据支持的中间区间，且更容易暴露于坏消息延续和 theta。

## 2. 策略假设的证据等级

| 假设 | 证据等级 | 判断 |
|---|---|---|
| 高量急跌可能包含临时流动性压力 | 中高 | 有理论与实证支持，但交易成本会吞噬一部分收益。[^3][^4][^7] |
| 基本面新闻驱动的下跌比非基本面下跌更不易反转 | 高 | 是 long-side reversal 的核心分层变量。[^5] |
| shock 后等待卖压衰减可降低接飞刀风险 | 中 | 因果方向合理；当前 2 日和 0.85 阈值仍是待验证先验。 |
| 上方 volume-profile “真空”带来反弹空间 | 低到中 | 可作为交易几何和止盈约束；尚无足够证据把它当独立 alpha。 |
| FINRA daily short-volume ratio 下降代表 bearish thesis 撤退 | 低 | FINRA 数据不是 short interest，也不覆盖全部交易场所。[^9] |
| unsigned OI×gamma 可以识别 dealer gamma 支撑/阻力 | 低 | OI 不含 dealer 持仓方向；严谨研究依赖 gamma imbalance 与流动性的交互。[^10] |
| IV 已正常化后买入比在 panic 峰值买入更合理 | 中高 | 与 long option 的正 vega/负 theta 以及负 volatility risk premium 证据一致。[^2][^11] |
| 0.25–0.45 delta、45–150 DTE、90 DTE 中心最优 | 低 | 是可解释的工程先验，不是已经验证的经济最优值。 |
| 1.25 ATR stop、55% premium stop、1.5R take profit 最优 | 低 | 风险纪律合理；具体数值与组合方式没有 OOS 证据。 |
| 二元胜率 + 目标/止损可以代表实际收益分布 | 低 | 与多路径、多退出、非正态的期权 P&L 不一致。 |

关键结论是：项目的**方向性因果链条是合理的**，但多数数值门槛仍是治理型先验，而非 alpha 证据。优化时应先修复语义和风险不一致，再做参数搜索；若反过来直接优化数值，很容易把结构错误拟合成漂亮回测。

## 3. 不同组件的一致性审计

### 3.1 Panic → compression → reversal：基本一致

当前 `panic_confirmed → min_post_shock_sessions → post-shock volume decay → ATR/RV compression → reversal/no-new-low` 的顺序是正确方向。它避免把 shock bar 自身的高量下跌误叫做 compression，也避免在 panic 未确认时直接把普通低波动识别成反弹准备。

仍有三个待验证点：

- `panic_metrics` 只在最近 5 根日 K 寻找 shock，因此实际可观察的 post-shock 窗口最多 4 个 session；这把策略天然限定为 very short-term。
- 等待至少 2 日可能降低 false bottom，也可能错过文献中最快的一至数日修复。这个取舍必须按 shock+1、+2、+3、+4 分 cohort 研究，不能只优化一个聚合结果。
- 当前 drawdown 是 raw return，没有扣除 SPY、行业 ETF 或 factor return。在全市场暴跌中，它可能把系统性 beta 当成个股错位。建议增加 market/sector residual shock，但保留 raw shock 供审计。

### 3.2 Fundamental gate：经济上最重要，当前操作上尚不可用

基本面 gate 与文献高度一致。Da 等的结果意味着，把 cash-flow news 与临时价格压力分开，会显著提高短期 reversal 信号纯度。[^5] 这不是一个可有可无的“AI 加分项”，而是决定样本属于“短期回弹”还是“永久重估/继续下跌”两种分布的 regime classifier。

当前本地 16 份 fundamental snapshot 全部为 `thesis_status=uncertain` 且 `needs_review=true`；部分虽有 URL，仍未满足可执行信心。系统 fail-closed 是正确的，但这表示 fundamental component 目前只完成了**安全否决**，没有完成**可用分类**。另外，代码允许 `thesis_status=uncertain` 通过 `thesis_not_broken`，只是通常又会被 `needs_review/confidence` 拦住。为避免未来字段组合意外放行，entry 应显式要求 `thesis_status == intact`。

建议把 fundamental 输出从单一 thesis 状态扩为互斥事件分类：

1. `LIQUIDITY_OR_TECHNICAL_DISLOCATION`；
2. `EARNINGS_OR_GUIDANCE_REPRICING`；
3. `BALANCE_SHEET_OR_FINANCING_BREAK`；
4. `ACCOUNTING_REGULATORY_LITIGATION`；
5. `M&A_OR_CAPITAL_ACTION`；
6. `UNKNOWN`。

发生 shock 时强制刷新，而不是继续依赖最多 7 日缓存。历史回测不能只给通用模型一个截止日期并假设它没有未来记忆；应向模型提供严格封存的 point-in-time 文档包，要求每个结论绑定当时已公开的原始资料。已有研究专门指出，LLM 在历史金融预测中可能因训练记忆产生 look-ahead bias。[^12]

### 3.3 Short component：方向合理，数据语义不足以做必选 gate

“知情做空没有继续增强”作为反弹辅助条件是合理的，因为机构型 short flow 对未来负收益具有信息。[^13] 但项目用到的 FINRA daily short volume 只覆盖向 FINRA 设施报告并公开传播的 off-exchange 交易；它不等于日终空头仓位，也未与交易所数据完整合并，部分抵消买单不会出现在文件中。FINRA 官方对此有明确提醒。[^9]

所以 `short_volume_ratio 相对 20 日均值下降 3pp` 不应在没有消融证据前成为所有 entry 的必选 gate。更稳妥的层级是：

- 新增/持续明显 short deterioration：veto 或强负面 modifier；
- short weakening：排序/置信度 modifier；
- 数据缺失：记录为 unknown，不冒充 bullish；
- 真正的 short-interest 存量与 daily flow 分开建模。

### 3.4 Volume profile 与 gamma：适合作为 geometry，不适合作为因果 alpha

Volume profile 当前最合理的用途，是回答“若反弹，最近的可实现结构目标在哪里”和“目标前是否有足够 room”，而不是预测反弹必然发生。它应参与 reward/risk 几何，但不应补偿 fundamental、liquidity 或 option-cost 失败。

Gamma concentration 的谨慎标签是正确的：普通 OI 对每张合约同时包含多空双方，无法识别 dealer 在哪一侧。学术结果讨论的是**带方向的 aggregate dealer gamma imbalance 与标的流动性的交互**；正/负 imbalance 对应的 intraday reversal/momentum 不同。[^10] 因此当前 unsigned `OI × gamma` 最多是“仓位集中价格带”，不应被叫作 gamma support，也不应决定 target，除非取得可验证的 signed flow/dealer-side 估计。

### 3.5 Option panic 与 entry surface：数值可重叠，事件时间不一致

现在 option panic 与 entry surface 都在 `as_of` 当前链面上计算。前者希望看到 IV jump、skew stress、term inversion 或 put-flow；后者又要求 skew、term、IV percentile 和 IV/YZ 已不过度昂贵。虽然阈值区间存在数学重叠，所以不一定永远互斥，但它混淆了两件事：

- **shock 当时**是否真的有 option panic；
- **准备入场时**期权价格是否已正常化到可买。

正确结构应保存 `shock_day_option_snapshot`，再用独立的 `entry_day_option_snapshot` 判断 normalization，例如 IV 从 shock peak 回落多少、skew/term 是否恢复、但标的 reversal 是否仍未完成。否则一个数日前的 price shock 可能被今天的 option surface “补确认”，也可能要求同一链面既恐慌又便宜。

此外，surface 目前来自 OI/volume 筛选并集，不是完整 constant-maturity chain。25-delta skew、45/120 DTE term 和 put/call ratio 都会受到筛选覆盖率变化影响。应记录每个 expiry/right/delta bucket 的 coverage；覆盖不足时 surface 为 `UNKNOWN`，而不是计算一个看似精确的值。

## 4. 开仓与期权选择的核心冲突

### 4.1 Alpha 是方向反弹，不是纯波动率套利

未对冲 long call 的局部 P&L 可以概念化为：

`Δ × dS + 1/2 × Γ × dS² + Vega × dIV + Theta × dt − spread − fees`

当前目标场景中 `dS > 0`，但 `dIV < 0`、`dt > 0`。因此正 delta/gamma 必须战胜负 vega 变化、theta 和交易成本。Coval 与 Shumway 说明期权收益并非简单等同于标的杠杆收益；其 straddle 结果也显示波动率风险本身被定价。[^14] Bakshi 与 Kapadia 的 delta-hedged 结果支持负 volatility risk premium，且高波动时期 long-option 的非方向部分表现更差。[^11] Goyal 与 Saretto 则表明，历史 realized volatility 与 ATM IV 的差异对期权横截面收益有解释力。[^15]

所以“先等 IV 正常化”是正确方向，但目前 `IV percentile ≤ 85%` 和 `IV/YZ ≤ 2.25` 过宽，不能单独证明 call 在预期 IV crush 后有正收益。真正的 gate 应是**按可执行 ask 买入后，在多个 IV/时间路径下仍有足够净 payoff**。

### 4.2 当前合约排序先优化流动性，不优化交易命题

当前流程先筛选 45–150 DTE、0.25–0.45 delta、OI/volume，然后按：

1. 最窄 spread；
2. delta 最接近 0.40；
3. DTE 最接近 90；

选择一张合约，之后才计算 target/stop scenario。spread 应是硬约束，但不应成为通过约束后压倒一切的目标函数。这样可能选择“最好成交但到目标仍亏损”的合约，并在更后面失败；理论上也可能漏过经济性更好的候选。

当前 2026-09-09 快照是一个有用的诊断，而不是绩效证据：在 16 个标的中，只有 BRZE 3 张、CHWY 4 张、DLTR 3 张、PINS 8 张、TTAN 3 张、XERS 1 张 Call 同时满足现有 DTE/delta/OI/volume；再加 10% spread 与 premium/spot 限制后只剩 TTAN 1 张，且没有任何一张在“15 日到结构目标 + IV crush 15% + 执行折价”下产生正目标 payoff。即使暂时放开 delta，当前可交易的 TTAN 0.54 delta Call 到目标的模型价仍低于 ask。这个结果说明系统拒绝交易是正确的，也显示当前标的目标幅度与裸 Call 成本经常不匹配。

### 4.3 应从“选 Call”升级为“选表达工具”

| 表达工具 | 与短期反弹的匹配 | 主要优点 | 主要问题 |
|---|---|---|---|
| 股票 | 高 | 无 theta/vega，收益与标的直接一致 | 资本占用大、下行不天然限于 premium |
| 较高 delta Call | 中高 | 更像股票，IV crush 相对不容易压过 delta | 权利金更高、凸性比例降低 |
| 0.25–0.45 delta Call | 条件性 | 资本较少、上行凸性强 | 需要更快更大反弹，对 IV crush/theta/spread 敏感 |
| Bull call debit spread | 高，若 target 明确且有限 | 上方 short call 可抵消部分 vega/theta 与成本；收益上限和结构目标一致 | 两腿 spread、成交与美式提前指派/腿风险更复杂 |

OCC 对 bull call spread 的标准描述也是“有限风险、有限收益”；其净波动率与时间损耗影响取决于 strikes。[^16] 这恰好与当前“最邻近结构目标 + 1.5R/分批止盈”的有限目标叙事更一致。但不能仅凭这一逻辑直接切换：两腿的真实组合 bid/ask、成交概率、提前指派和操作复杂度必须纳入研究。

推荐的选择器顺序是：

1. 对所有候选 instrument/contract 施加新鲜度、OI、volume、spread、最小报价和可下单约束；
2. 对每张合约或组合计算至少四条路径：快速反弹、慢速反弹、横盘、继续下跌；每条包含 IV 路径、theta、entry ask、exit bid 与费用；
3. 先做鲁棒性 gate，例如“目标场景净收益为正、横盘损失可接受、继续下跌按保守 bid 不突破风险预算”；
4. 再在通过者中按稳健 payoff、资本效率和流动性排序；
5. 冷启动不输入未经验证的概率；有 OOS 分布后才计算 expected utility/Kelly。

DTE 与 delta 不应立刻改成另一个固定值。应预注册少量经济上不同的 bucket，例如高/中 delta 与短/中 DTE，做 walk-forward 对比。最终可能保留 0.25–0.45 delta，但它必须证明在真实 target 幅度与 IV crush 后优于股票、高 delta Call 或 debit spread。

## 5. 当前规则隐含的收益分布

### 5.1 不是正态，也不是简单的“有限亏损、无限盈利”

策略层面的结果更像一个 regime mixture：

| 隐状态 | 标的路径 | IV 路径 | 典型期权结果 | 当前退出 |
|---|---|---|---|---|
| 临时流动性错位、快速修复 | 数日快速上涨 | 从 panic 高点回落 | delta/gamma 可能覆盖 crush，正 R | 结构目标或 1.5R |
| 方向正确但反弹慢/小 | 缓慢上涨或横盘 | 回落 | 标的上涨但期权仍亏或小赚 | IV crush、time/DTE stop |
| false bottom | 先小反弹后再跌 | 先降后升 | 路径依赖亏损 | premium/underlying stop |
| 基本面永久重估 | 继续下跌或 gap | 可维持高 IV | call 的 delta 损失占主导 | thesis/underlying stop；可能大幅滑点 |
| 意外强反弹/gap up | 快速大涨 | 任意 | 裸 Call 的大右尾 | 单张在结构目标/1.5R 即全平，右尾被截断 |

裸长 Call 本来具有正凸性和潜在右尾，但当前策略对单张冷启动仓位在结构目标或 1.5R 全部退出；与此同时，gap、无 bid 和宽 spread 可令实际亏损接近全部权利金。故**最终策略分布的正偏特征被明显削弱**，偏度正负不能靠“买了 Call”推断，必须用真实路径和可执行报价测量。

### 5.2 1R 目前不是稳定的实际最大损失

scenario pricing 当前把 option stop 定义为：

`min(Black–Scholes hard-stop scenario × 0.95, entry ask × 45%)`

这里的 `min` 意味着如果模型 stop 只有 ask 的 20%，冻结 stop 就是 20%，对应 80% premium loss，而不是最多 55%。所以 `option_premium_stop_pct=0.55` 实际上不是损失上限，只是 stop price 的一个上界。之后 sizing 又用 `(entry − stop)` 定义 risk per contract，于是建仓 1R 可能低于真实 gap/流动性尾损失。

这应拆成三个字段：

- `thesis_stop_spot`：标的价格使 reversal thesis 失效；
- `planned_option_exit_bid`：在该标的与 IV 场景下预计可执行的 option bid；
- `capital_at_risk`：组合硬限制使用的保守风险。对 long option，最稳健定义是全部 premium + fees；若另设 operational stop，必须明确它不保证最大亏损。

Kaminski 与 Lo 证明 stop-loss 的价值依赖收益生成过程：随机游走下会降低期望收益，momentum/regime-switching 下可能有帮助。[^17] 对均值反转策略，stop 最合理的角色是**识别 thesis 已从 reversal regime 切换为 continuation regime**，而不是把一个未经验证的固定百分比当作无条件增益。

### 5.3 止盈使 Kelly 的二元近似失真

当前退出不止 win/lose 两种：结构目标、option target、1.5R、profit giveback、IV crush、short reacceleration、time stop、DTE、thesis break 和两类 stop 都能结束交易。收益还取决于路径、spread 和是否分批。用一个全局胜率 `p` 和单一 `b = gain/loss` 做 Kelly，会把多状态分布压扁为错误的二项分布。

若假设赔率固定为 `b`，二元 Kelly 的盈亏平衡胜率是 `1/(1+b)`：

- `b=1.5` 时需要胜率大于 40%；
- `b=1.0` 时需要大于 50%；
- `b=0.5` 时需要大于 66.7%。

冷启动当前只要求 target payoff 为正，没有最低 reward/risk，也没有概率，所以一个 `b=0.1` 的场景理论上仍可进入固定风险 sizing。固定风险本身没有错，但 entry 仍需预注册一个稳健 payoff gate；阈值应由 OOS 研究决定，而不是把 1.5R take-profit 误当作每笔交易天然有 1.5 的建仓赔率。

验证模式应改为直接使用经验多状态分布：

`f* = argmax_f Σ p_j log(1 + f × R_j)`

并对参数不确定性、相关性和 drawdown 加约束。即使继续使用 fractional Kelly，也必须使用 OOS `R_j` 分布或显著收缩后的条件分布，而不是单一全局胜率。

### 5.4 30 笔完成交易不足以启动 Kelly

当前配置把 30 笔 OOS 完成交易作为 Kelly 的最低条件。即便暂时忽略事件相关性，在真实胜率约 50% 时，30 个独立 Bernoulli 观测的普通 95% 误差带约为 ±18 个百分点；要把误差压到约 ±5 个百分点，数量级接近 384 个独立观测。真实事件会在市场/行业/财报周聚类，有效样本量更低。

所以 30 笔适合作为工程 smoke test，不适合为 Kelly 提供稳定概率。Kelly readiness 应同时要求：足够多的独立事件、多个市场 regime、置信下界为正的 after-cost expectancy、稳定的尾损失、以及严格记录研究过程中尝试过的参数数量。Bailey 与 López de Prado 的 Deflated Sharpe Ratio 正是为 selection bias、多重试验和非正态收益修正绩效膨胀。[^18] Harvey、Liu 与 Zhu 也说明，在大量因子/规则被测试时，传统显著性门槛过低。[^19]

## 6. 平仓、止盈止损的一致性问题

### 6.1 做得好的部分

- 退出按当前可执行 bid 标记，而不是 mid/last。
- entry ATR、标的 hard stop、option stop、target、IV、DTE 与 max hold 在开仓时冻结，避免事后移动规则。
- expiry、thesis break、标的 hard stop 和 time stop 不因 ancillary data 缺失而被隐藏。
- 冷启动只有 1 张，系统不会假装可以分批保留 runner。

### 6.2 需要修复的部分

1. **结构目标与 option economics 不完全一致**：标的触及 profile/gamma 目标即退出，即使期权因 IV crush 仍亏；这可能是正确止盈纪律，但必须在建仓 scenario 中按“触及时的可执行 bid”评价，而不能只看终点理论价。
2. **1.5R 截断凸性**：如果选择裸 Call 的理由是大右尾，单张仓位 1.5R 全退会削弱该优势；如果策略本来只要有限结构目标，debit spread 或高 delta 工具可能更一致。
3. **多张分批缺少一次性状态**：若意图只在首次 target 减仓一次，当前仓位没有 `target_taken/remaining_runner` 状态；成交回写后下一轮仍满足 target 时，可能继续减仓。
4. **日线与日内规则不完全同构**：日线有 IV crush、short reacceleration、profit giveback 等逻辑，日内只处理硬风险、premium stop 与目标。若这些触发器需要实时执行，应共享同一状态机；若只允许日频判断，应在文档中明确其延迟风险。
5. **spread 门槛不一致**：研究 entry 10%、broker entry 12%、日线 premium-stop 可靠性约 15%、intraday premium-stop 30%。不同用途可以有不同阈值，但必须命名为不同风险层，并用相同的 bid/ask 数据回测。
6. **premium stop 宽 spread 时等待**：等待可避免在虚假宽价差中低卖，但也会让“55% stop”失去风险边界。应升级为 alert + 可配置的 marketable-limit/人工接管流程，且组合风险仍按全 premium 计算。

OCC 的标准风险文件强调 option holder 可能损失全部权利金，且复杂 spread 还带有执行与指派风险；任何理论 stop 都不能替代这一资本风险定义。[^20]

## 7. 组合层的一致性

当前组合 gate 只汇总：权利金、冻结 stop 风险、持仓数、同一 underlying 数量。对冷启动 1 张/1 仓，这是一个合理的第一层保护；对未来 3 个持仓和 Kelly 模式则不够，因为所有仓位很可能同时是：

- long market/sector beta；
- long delta；
- long vega；
- long gamma；
- 对财报、宏观、流动性恶化和 option spread 扩张高度相关。

三个不同 ticker 不等于三个独立风险。未来 portfolio capacity 至少应增加：

- net/gross delta dollar exposure；
- vega、gamma 与 theta；
- sector 与 market beta concentration；
- earnings/catalyst date clustering；
- liquidity stress：所有持仓以 bid 下移、spread 扩大时的退出损失；
- shock correlation：同一天/同一行业产生的 signals 视为一个 cluster。

组合限制仍应用 conservative entry capital，而不是动态上涨后的市值；但每日风险报告需要同时展示 cost basis、当前 bid value 与全 premium tail loss。

## 8. 推荐的目标策略规格

### 8.1 核心 mandate

建议把内部 mandate 明确为：

> **Short-Horizon Idiosyncratic Panic-Dislocation Rebound with Directional Convexity**

“LongVol”可保留为项目名，但所有报告应说明它不是 delta-neutral long-vol strategy。核心预测变量是未来数日的 residual spot rebound；option 是表达工具，不是 alpha 本身。

### 8.2 建议保留的 hard veto

- point-in-time 数据不完整或时间戳不可验证；
- 没有同一 shock bar 上的异常流量与价格/区间冲击；
- shock 后 selling pressure 未衰减或仍创新低；
- fundamental 不是明确 `intact`，或属于 earnings/guidance、融资、会计、监管等永久重估类；
- entry instrument 在 ask→bid、IV crush、费用后目标 payoff 非正；
- spread/报价/整张合约风险不可接受；
- portfolio stress 超限。

### 8.3 建议先降级为 feature/modifier 的项目

- exact Yang–Zhang ratio threshold；
- FINRA short-volume weakening；
- volume-profile vacuum 密度阈值；
- unsigned gamma concentration；
- 单一 IV percentile 阈值；
- 固定 delta/DTE 中心。

这些变量可以保留记录，并在只通过核心 veto 的样本中做排序和消融。只有在稳定 OOS 增益出现后，才升级为 hard gate。这样既避免 gate 太多导致几乎无样本，也避免弱证据组件把好 setup 全部否决。

### 8.4 建议的主持有窗口

不要直接把 30 日改成另一个拍脑袋数字。研究时应同时报告 shock 后 1、2、3、5、10、15、20 个**交易日**的 underlying 与 option 结果，并注意当前 pricing 中的 `target_horizon_days=15` 实际按**日历日**折算剩余期限。若证据与当前文献相符，主退出窗口很可能集中在 3–10 个交易日，15 个交易日作为慢速反弹研究上界，30 个日历日仅保留为异常兜底。最终参数必须由 walk-forward OOS 决定。

## 9. 验证收益分布的研究设计

### 9.1 事件样本

每个独立 panic event 是一条样本，不应把同一标的连续数日的重复 signal 当成独立交易。事件表至少包含：

- point-in-time universe，包括之后退市、并购和破产的证券；
- 未复权/复权价格、split/dividend/corporate action；
- event 当时可见的 SEC/issuer/regulator/news 文档及发布时间；
- 当时完整 option chain、NBBO/可用 bid-ask、Greeks、OI、volume、quote timestamp；
- SPY、行业 ETF、VIX 与 market liquidity regime；
- 所有 gate 原值、未知值和拒绝原因。

### 9.2 标签与收益口径

同时保存两个层次：

1. **underlying alpha labels**：residual return、raw return、MAE/MFE、到 target/stop 的 first-passage time，期限 1/2/3/5/10/15/20 日；
2. **instrument labels**：entry ask、每个退出时点的 bid、费用、spread stress、IV/Greeks 变化、实际规则 R、多腿组合可成交价格。

必须先证明 underlying reversal edge 存在，再判断哪个 instrument 把它转成最高 after-cost utility。否则期权亏损时无法知道是 alpha 错了，还是工具太贵。

### 9.3 最小实验矩阵

| 维度 | 预注册对照 |
|---|---|
| shock 后入场 | +1、+2、+3、+4 session；分别要求/不要求 compression |
| return 定义 | raw、market residual、sector residual |
| fundamental | liquidity/unknown/earnings-guidance/structural-break |
| instrument | stock、高 delta Call、中 delta Call、bull call spread |
| DTE | 与目标窗口匹配的 2–3 个宽 bucket |
| exit | fixed horizon、structure first-passage、thesis stop、time stop |
| short/gamma/profile | 逐项 ablation，不一次同时加入 |

### 9.4 统计与验收

- 使用 chronological walk-forward；所有 scaler、阈值与合约选择只在训练段确定。
- 对重叠持有期和同日/同行业事件做 purge、embargo 或 cluster bootstrap。
- 记录每次尝试过的规则，报告 Probability of Backtest Overfitting、Deflated Sharpe/selection-adjusted 指标，而不只报告最佳组合。[^18][^19][^21]
- 核心指标：after-cost mean/median R、win rate、profit factor、5% CVaR、最大 drawdown、time-under-water、spread sensitivity、按 regime 的稳定性。
- 报告完整分布和置信区间，不只报告 Sharpe；option 策略天然非正态且有 path dependence。
- Kelly 必须最后启用；首先要求固定风险模拟盘的分布与历史 OOS 同方向，且概率/赔率估计在 regime 间稳定。

## 10. 优化路线图

### P0：立即执行，不需要拟合

1. 继续保持 `SIMULATE` 与 `COLD_START_FIXED_RISK`；当前没有任何证据支持 Kelly。
2. 在文档和信号中增加 `strategy_horizon=SHORT_TERM`、`alpha_source=SPOT_REVERSAL`、`instrument_exposure=DIRECTIONAL_CONVEXITY`。
3. 组合硬风险按 long-option 全 premium + fees 计算；另保留 planned stop risk，不再把两者混为一谈。
4. fundamental entry 显式要求 `intact`；shock 触发强制 point-in-time refresh。
5. 保存 shock-day option snapshot，与 entry-day surface 分离。

### P1：结构修复，仍不优化参数

1. 改为先枚举全部合约/工具、计算 scenario，再选择；spread 是约束，不是第一目标函数。
2. 将 option scenario 扩为快速/慢速/横盘/继续下跌四路径，全部使用 ask→bid 和费用。
3. 将 `thesis_stop_spot`、`planned_option_exit_bid`、`capital_at_risk` 分栏。
4. 统一 daily/intraday exit 状态机；增加一次性 partial-take 状态与异常 spread 升级流程。
5. execution age 改成“下一个合格交易 session”，而不是仅用日历日差；执行前重验 spot、surface、fundamental freshness 和 payoff。
6. gamma 暂时只做 diagnostic；short weakening 降为 modifier，明显 deterioration 保留 veto。

### P2：研究决定参数

1. 建立完整事件面板与可执行 option labels。
2. 预注册小规模实验矩阵，进行 gate ablation 与 instrument tournament。
3. 决定 post-shock wait、主持有窗口、delta/DTE、target、stop 和最低稳健 reward/risk。
4. 检查结果是否仅由少数年份、财报事件、小盘股或宽 spread 合约贡献。

### P3：模拟盘验收与 Kelly

1. 先证明固定风险 paper execution 与 backtest 的 slippage、fill rate、MAE/MFE 一致。
2. 累积足够多的独立完成事件，而不是仅达到 30 笔。
3. 使用多状态 OOS R 分布、置信收缩与 portfolio correlation 计算 risk-constrained fractional Kelly。
4. 任何策略版本、数据源、fundamental classifier 或 exit 语义变化都使旧 Kelly evidence 失效。

## 11. 最终判断

LongVol 的**核心研究方向是合理的**：异常量价急跌、基本面剥离、卖压衰减、反转确认、交易成本与期权可执行性，是一条可以被证伪的短期 liquidity-reversal 逻辑。它目前最强的部分是风险治理与 fail-closed，而不是已证明的 alpha。

当前组件不是完全一致。最严重的问题是用正 vega、负 theta 的中低 delta 长 Call 去表达一个预期 IV crush 的有限幅度短期反弹，同时用结构目标/1.5R 截断上行，却没有把 full-premium/gap 风险计入同一个 1R。其次是 fundamental classifier 还没有产生可用的事件分层，option panic 与 entry normalization 也没有按事件时间分开。

因此，下一版不应首先“调松 gate 以产生更多 candidate”，也不应首先优化 panic/compression 阈值。合理顺序是：**明确 short-term mandate → 分离事件与入场快照 → 分离 alpha 与 instrument → 修正风险定义 → 建立可执行收益分布 → 最后才校准门槛与 Kelly**。在这些步骤完成前，0 笔交易不是失败，而是系统正确拒绝了尚未证明经济性和数据完整性的 setup。

## 脚注

[^1]: Werner F. M. De Bondt and Richard Thaler, “[Does the Stock Market Overreact?](https://doi.org/10.1111/j.1540-6261.1985.tb05004.x),” *Journal of Finance* 40(3), 1985.
[^2]: Options Industry Council, “[Long Call](https://www.optionseducation.org/strategies/all-strategies/long-call),” accessed 2026; see also OCC, *Characteristics and Risks of Standardized Options*.
[^3]: Stefan Nagel, “[Evaporating Liquidity](https://www.nber.org/papers/w17653),” NBER Working Paper 17653, 2011; published in *Review of Financial Studies*, 2012.
[^4]: John Y. Campbell, Sanford J. Grossman, and Jiang Wang, “[Trading Volume and Serial Correlation in Stock Returns](https://www.nber.org/papers/w4193),” NBER Working Paper 4193, 1992; published in *Quarterly Journal of Economics*, 1993.
[^5]: Zhi Da, Qianqiu Liu, and Ernst Schaumburg, “[A Closer Look at the Short-Term Return Reversal](https://doi.org/10.1287/mnsc.2013.1766),” *Management Science* 60(3), 2014.
[^6]: Don R. Cox and David R. Peterson, “[Stock Returns following Large One-Day Declines](https://doi.org/10.1111/j.1540-6261.1994.tb04428.x),” *Journal of Finance* 49(1), 1994.
[^7]: Doron Avramov, Tarun Chordia, and Amit Goyal, “[Liquidity and Autocorrelations in Individual Stock Returns](https://doi.org/10.1111/j.1540-6261.2006.01060.x),” *Journal of Finance* 61(5), 2006.
[^8]: Narasimhan Jegadeesh and Sheridan Titman, “[Momentum](https://doi.org/10.1146/annurev-financial-102710-144850),” *Annual Review of Financial Economics* 3, 2011.
[^9]: FINRA, “[Short Sale Volume](https://www.finra.org/finra-data/browse-catalog/short-sale-volume)” and “[Information Notice 5/10/19](https://www.finra.org/rules-guidance/notices/information-notice-051019).”
[^10]: Andrea Barbon and Andrea Buraschi, “[Gamma Fragility](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3725454),” University of St. Gallen Research Paper 2020/05, revised 2021.
[^11]: Gurdip Bakshi and Nikunj Kapadia, “[Delta-Hedged Gains and the Negative Market Volatility Risk Premium](https://doi.org/10.1093/rfs/hhg002),” *Review of Financial Studies* 16(2), 2003.
[^12]: Paul Glasserman and Caden Lin, “[Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis](https://arxiv.org/abs/2309.17322),” arXiv, 2023.
[^13]: Ekkehart Boehmer, Charles M. Jones, and Xiaoyan Zhang, “[Which Shorts Are Informed?](https://doi.org/10.1111/j.1540-6261.2008.01324.x),” *Journal of Finance* 63(2), 2008.
[^14]: Joshua D. Coval and Tyler Shumway, “[Expected Option Returns](https://doi.org/10.1111/0022-1082.00352),” *Journal of Finance* 56(3), 2001.
[^15]: Amit Goyal and Alessio Saretto, “[Cross-section of Option Returns and Volatility](https://doi.org/10.1016/j.jfineco.2009.01.001),” *Journal of Financial Economics* 94(2), 2009.
[^16]: Options Clearing Corporation, “[Options Strategies Quick Guide](https://www.theocc.com/getcontentasset/f34f8a0d-806f-4f1a-adf7-d49d8d94b16e/dfc3d011-8f63-43f6-9ed8-4b444333a1d0/option-strategies-quick-guide.pdf),” bull call spread section.
[^17]: Kathryn M. Kaminski and Andrew W. Lo, “[When Do Stop-Loss Rules Stop Losses?](https://doi.org/10.1016/j.finmar.2013.07.001),” *Journal of Financial Markets* 18, 2014.
[^18]: David H. Bailey and Marcos López de Prado, “[The Deflated Sharpe Ratio](https://doi.org/10.2139/ssrn.2460551),” *Journal of Portfolio Management* 40(5), 2014.
[^19]: Campbell R. Harvey, Yan Liu, and Heqing Zhu, “[... and the Cross-Section of Expected Returns](https://www.nber.org/papers/w20592),” NBER Working Paper 20592, 2014; published in *Review of Financial Studies*, 2016.
[^20]: Options Clearing Corporation, “[Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document),” June 2024 edition.
[^21]: David H. Bailey, Jonathan M. Borwein, Marcos López de Prado, and Qiji Jim Zhu, “[The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf),” *Journal of Computational Finance* 20(4), 2017.

## Sources

1. De Bondt, Werner F. M., and Richard Thaler. “[Does the Stock Market Overreact?](https://doi.org/10.1111/j.1540-6261.1985.tb05004.x)” *Journal of Finance* 40(3), 1985.
2. Options Industry Council. “[Long Call](https://www.optionseducation.org/strategies/all-strategies/long-call).” Accessed 2026.
3. Nagel, Stefan. “[Evaporating Liquidity](https://www.nber.org/papers/w17653).” NBER Working Paper 17653, 2011.
4. Campbell, John Y., Sanford J. Grossman, and Jiang Wang. “[Trading Volume and Serial Correlation in Stock Returns](https://www.nber.org/papers/w4193).” NBER Working Paper 4193, 1992.
5. Da, Zhi, Qianqiu Liu, and Ernst Schaumburg. “[A Closer Look at the Short-Term Return Reversal](https://doi.org/10.1287/mnsc.2013.1766).” *Management Science* 60(3), 2014.
6. Cox, Don R., and David R. Peterson. “[Stock Returns following Large One-Day Declines](https://doi.org/10.1111/j.1540-6261.1994.tb04428.x).” *Journal of Finance* 49(1), 1994.
7. Avramov, Doron, Tarun Chordia, and Amit Goyal. “[Liquidity and Autocorrelations in Individual Stock Returns](https://doi.org/10.1111/j.1540-6261.2006.01060.x).” *Journal of Finance* 61(5), 2006.
8. Jegadeesh, Narasimhan, and Sheridan Titman. “[Momentum](https://doi.org/10.1146/annurev-financial-102710-144850).” *Annual Review of Financial Economics* 3, 2011.
9. FINRA. “[Short Sale Volume](https://www.finra.org/finra-data/browse-catalog/short-sale-volume).”
10. FINRA. “[Information Notice 5/10/19](https://www.finra.org/rules-guidance/notices/information-notice-051019).” 2019.
11. Barbon, Andrea, and Andrea Buraschi. “[Gamma Fragility](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3725454).” University of St. Gallen Research Paper 2020/05, revised 2021.
12. Bakshi, Gurdip, and Nikunj Kapadia. “[Delta-Hedged Gains and the Negative Market Volatility Risk Premium](https://doi.org/10.1093/rfs/hhg002).” *Review of Financial Studies* 16(2), 2003.
13. Glasserman, Paul, and Caden Lin. “[Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis](https://arxiv.org/abs/2309.17322).” arXiv, 2023.
14. Boehmer, Ekkehart, Charles M. Jones, and Xiaoyan Zhang. “[Which Shorts Are Informed?](https://doi.org/10.1111/j.1540-6261.2008.01324.x).” *Journal of Finance* 63(2), 2008.
15. Coval, Joshua D., and Tyler Shumway. “[Expected Option Returns](https://doi.org/10.1111/0022-1082.00352).” *Journal of Finance* 56(3), 2001.
16. Goyal, Amit, and Alessio Saretto. “[Cross-section of Option Returns and Volatility](https://doi.org/10.1016/j.jfineco.2009.01.001).” *Journal of Financial Economics* 94(2), 2009.
17. Options Clearing Corporation. “[Options Strategies Quick Guide](https://www.theocc.com/getcontentasset/f34f8a0d-806f-4f1a-adf7-d49d8d94b16e/dfc3d011-8f63-43f6-9ed8-4b444333a1d0/option-strategies-quick-guide.pdf).”
18. Kaminski, Kathryn M., and Andrew W. Lo. “[When Do Stop-Loss Rules Stop Losses?](https://doi.org/10.1016/j.finmar.2013.07.001).” *Journal of Financial Markets* 18, 2014.
19. Bailey, David H., and Marcos López de Prado. “[The Deflated Sharpe Ratio](https://doi.org/10.2139/ssrn.2460551).” *Journal of Portfolio Management* 40(5), 2014.
20. Harvey, Campbell R., Yan Liu, and Heqing Zhu. “[... and the Cross-Section of Expected Returns](https://www.nber.org/papers/w20592).” NBER Working Paper 20592, 2014.
21. Options Clearing Corporation. “[Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document).” June 2024.
22. Bailey, David H., Jonathan M. Borwein, Marcos López de Prado, and Qiji Jim Zhu. “[The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).” *Journal of Computational Finance* 20(4), 2017.
