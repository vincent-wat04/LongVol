# LongVol 0.7：美股短期恐慌错位反弹、条件期权模拟与 Moomoo 执行系统

LongVol 是在本地运行的短期个股错位反弹研究、长 Call 表达、模拟/实盘执行与审计系统。Moomoo Stock Screening V2 自动生成股票候选，Option Screening 缩小期权面；本地确定性代码负责 panic、post-shock compression、合约经济性、仓位、组合风险与退出；DeepSeek/OpenAI-compatible 模型只能对本地封存的 point-in-time 新闻包做基本面分类。

本版本不含截图、OCR、图片上传或候选池人工录入代码。工程具备 fail-closed、限价单、幂等订单、成交回写和模拟/实盘隔离。策略收益仍需用无前视数据完成样本外研究与模拟盘验收。

它不是长期价值反转，也不是 delta-neutral long-vol。核心 alpha 假设是未来数个交易日的 idiosyncratic spot rebound；期权只是带正 delta/gamma、同时承担 IV crush、theta 与 spread 的表达工具。

## v0.7 的关键设定

- 先枚举所有符合 DTE、delta、OI、volume、spread 与报价新鲜度约束的 Call，再对每张做 target/stop 情景与条件 Monte Carlo；不再先按最窄 spread 选一张。
- Monte Carlo 默认 `RESEARCH_ONLY`：expected return、P(profit)、VaR/CVaR 只作诊断；只有不依赖假设概率、且计入 ask→bid、IV 路径与费用的 target/stop 净经济性参与 gate。
- 股票状态是期权模拟的条件输入，不由期权模拟反推；当前三状态概率明确标记为 `UNVALIDATED_PRIOR`。
- 账户资金从 Moomoo 交易 API 同步，只按 USD 资金运行；HKD 不隐式换算。
- 默认 `SIMULATE`；`REAL` 需要配置、环境变量和命令行三重授权。
- 冷启动不使用 Kelly，也不制造历史：同一交易日 Moomoo `Option Underlying Rank` 的 IV percentile 与本地 IV 历史分开记录；默认只是诊断，只有显式启用 `require_iv_regime_gate` 才参与准入。
- 冷启动最多 1 张、单笔及组合 full-premium tail risk 5%、planned-stop risk 2.5%、最多 1 个持仓。USD 2,500 对应约 USD 125 满权利金风险和 USD 62.50 计划止损风险，两者独立约束。
- 本地 near-ATM IV 继续逐日独立累计，不会被 vendor percentile 回填或混合。
- 本地 IV 观测、获批样本外报告、报告 SHA-256、策略版本、样本天数及完成交易数全部合格后，按标的自动切到 `VALIDATED_KELLY`。
- volume profile、short weakening、unsigned gamma、market/sector residual 与单一 IV/surface 阈值保留为可审计 feature，默认不作 universal hard gate；新鲜 short deterioration 仍是 veto。
- fundamental 必须明确 `intact` 且由本地 PIT 新闻包支持 temporary-dislocation 分类；unknown/证据不足 fail closed。
- shock-day option panic 与 entry-day option surface 分开，当前链不能回溯确认旧 shock。
- 收盘后决定是否值得交易；日内只在下一个真实开市 session 检查成交质量与安全退出，不盘中重算 alpha thesis。

## 安装

要求 Python 3.11+、本机已登录的 Moomoo OpenD `10.10.7008`、美股与 OPRA OpenAPI 行情权限、对应交易账户，以及在启用自动基本面分类时可用的 DeepSeek/OpenAI-compatible API key。可交易的 fundamental gate 还需要至少一个合规且已确认实时 entitlement 的 broad-news provider；缺少它不会让 healthcheck 整体失败，但相关标的会在 daily 中 fail closed。旧版 OpenD（例如 `10.7.6708`）不支持本项目使用的 Option Screening V2 协议，不能只升级 Python SDK 而保留旧 OpenD。

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e '.[openai,moomoo]'

cp .env.example .env
# 写入 DeepSeek key、一个 news provider 和 SEC_USER_AGENT；
# REAL 模式另需 MOOMOO_TRADE_PASSWORD_MD5
set -a; source .env; set +a

longvol healthcheck --opend --broker --research \
  --trading-config config/trading.json
PYTHONPATH=src python -m unittest discover -s tests -v
```

直接依赖固定为 `openai==3.8.0`、`moomoo-api==10.10.7008` 与 `protobuf==5.29.5`。Moomoo 10.10.7008 仍使用 `FieldDescriptor.label`，与 protobuf 7.x 不兼容；healthcheck 会验证这一组合。升级 SDK 或 protobuf 前必须重测 screen、option rank、snapshot、交易账户、订单、分页和字段映射。

## 模拟盘与实盘

`config/trading.json` 默认使用 `SIMULATE`、USD 资金和 USD 2,500 策略资金上限。模拟盘须选择美股 `STOCK_AND_OPTION` 账户；如果 OpenD 返回多个匹配账户，程序拒绝猜测，必须配置稳定的 `account_id`。部分 Moomoo 模拟账户不下发逐币种 `netCashPower`，此时程序只在 `SIMULATE` 环境显式回退到 `us_cash` 作为非杠杆策略资金基数并记录原因；`REAL` 环境不会回退。

```bash
longvol account-sync --trading-config config/trading.json \
  --output state/broker/account.json \
  --local-positions data/positions.csv

# 先只校验，不提交
longvol place-order --intent data/order_intent_example.json \
  --trading-config config/trading.json

# 提交到模拟盘
longvol place-order --intent data/order_intent_example.json \
  --trading-config config/trading.json --submit
```

切换实盘必须同时满足：`environment=REAL`、真实 `account_id`、`allow_live_orders=true`、`MOOMOO_TRADE_PASSWORD_MD5`，以及每次执行的 `--live-confirmation I_UNDERSTAND_LIVE_ORDERS`。任何一项缺失都会阻止真实订单。所有订单均为 DAY 限价单；下单与撤单不自动重试，避免网络超时引起重复订单。

## 每日收盘流程

```bash
python scripts/run_daily.py \
  --config config/strategy.json \
  --sizing config/sizing.json \
  --trading-config config/trading.json
```

默认命令按纽约当天运行，并在 17:00 前拒绝同日收盘任务。若要在下一交易日盘前补跑上一完成交易日，必须在纽约 09:00 前显式指定日期：

```bash
python scripts/run_daily.py --as-of 2026-09-09 \
  --config config/strategy.json \
  --sizing config/sizing.json \
  --trading-config config/trading.json
```

盘前补跑要求日线最后日期、Moomoo IV regime 日期和每个被采用的期权 Market Snapshot `update_time` 均等于 `as_of`。Stock Screener V2 只产生候选池，所有 panic 数值由截至 `as_of` 的本地 K 线重算。当天 09:00 ET 后不允许补认昨日完整期权快照；更旧日期只能单独同步历史 K 线。`--allow-nonstandard-time` 仅处理同日半日市等人工例外，不用于绕过历史期权验证。单个候选没有上市期权或没有合约通过 OI/volume 并集时会生成空期权快照，并在 scan 中失败关闭为非候选，而不会中止其他标的；端点错误或不可验证的时间戳仍会中止同步。

完整流程为：账户同步与成交回写 → Stock Screener V2 → tracked universe → K 线、Option Screening、逐合约 Market Snapshot 时间验证、按日 option archive、Moomoo IV regime 与 short 同步 → append-only 新闻采集/PIT packet → 基本面事件分类 → 顺序门槛 → 全合约情景/MC → 自动仓位模式 → 两类组合风险 → exit。

### 新闻与基本面证据

当前 DeepSeek Responses 接口不能被当作新闻采集器：本部署不会执行 `web_search`，`web_search` 与 `mcp` 等不受支持的 built-in tool 可能被静默忽略。MCP 只是工具传输协议，也不提供新闻数据或许可。交易流水必须先由外部 SEC/Alpaca/Benzinga/Massive provider 采集、记录本地接收时间并封存，然后才把 evidence packet 交给模型分类。

Alpaca 与 direct Benzinga 的实时资格必须由账户 entitlement 和数据合同确认；默认均为关闭：

```bash
ALPACA_NEWS_REALTIME_ENTITLED=false
BENZINGA_NEWS_REALTIME_ENTITLED=false
```

只有确认实时权利后才能把相应值设为 `true`。在 cutoff 之后才抓到的历史文章即使 `published_at` 早于 cutoff，也只会追加到 archive，不能进入该历史日期的 PIT gate。

```bash
# 先采集并封存；research_packets/{symbol}.json|txt 只是可选本地 context
longvol news-sync --symbols TBBK,GPK \
  --start 2026-08-26 --as-of 2026-09-09 \
  --providers alpaca,sec --state-dir state \
  --local-packet-dir research_packets

# 显式、可能计费的能力探测；结果只用于运维发现，不是交易证据
longvol research-capabilities --billable-probe \
  --model deepseek-flash
```

新闻修订按 `state/news/archive/{symbol}.jsonl` 只追加保存；按 cutoff 重建的包位于 `state/news/packets/{as_of}/{symbol}.json`。`research_packets/` 不属于新闻 archive，也不能自行取得 evidence allowlist 资格。

推荐层级是：SEC EDGAR 作为免费一手 filing 底座但不代表 broad-news coverage；确认 realtime entitlement 后用 Alpaca News 做 MVP/shadow coverage；对延迟、长尾覆盖和修订要求更高时采购 direct Benzinga；Massive 基础新闻为小时级补充召回，不单独通过实时 fundamental gate。Healthcheck 只报告 provider/entitlement 配置且不发起付费探测；没有合格 broad provider 时，daily 仍继续处理批次，但单个标的基本面保持 `uncertain/UNKNOWN`。

### 自动仓位状态机

`config/sizing.json` 仅接受 `mode=AUTO`：

- `COLD_START_FIXED_RISK`：本地 IV 历史或 Kelly 研究未达标时启用；不需要也不读取胜率。若显式启用 IV gate，才要求当天 Moomoo IV percentile；所有其他 hard gate 通过后输出 `PILOT_CANDIDATE`。
- `VALIDATED_KELLY`：至少 60 个本地先验 IV 观测，且配置中有真实存在、哈希一致、版本一致、明确获批的样本外报告；输出 `BUY_CANDIDATE`。

默认 Kelly 证据为空，因此新安装一定处于冷启动。系统只会自动识别已经获批的证据，不会自动批准报告、估计胜率或补历史数据。每次判断与模式变化均写入 SQLite 的 `sizing_mode_decisions` 和 `events`。

## 日内执行监控

日内检测有必要，但只作为 execution overlay：不重算 panic、compression 或基本面。

```bash
# 单次 dry-run
longvol intraday-monitor \
  --signals state/signals/2026-09-09.csv \
  --positions data/positions.csv \
  --trading-config config/trading.json \
  --output state/intraday/latest.json

# 模拟盘持续轮询与提交
longvol intraday-monitor \
  --signals state/signals/2026-09-09.csv \
  --positions data/positions.csv \
  --trading-config config/trading.json \
  --output state/intraday/latest.json --submit --loop
```

默认每 60 秒检查，入场窗口为美东 10:00–15:30。要求信号未过期、标的与期权报价不超过 20 秒、spread/mid ≤ 12%、live ask 相对信号 ask 漂移 ≤ 10%、不追涨超过 0.5 ATR，且止损、目标与最新同步 USD 资金仍允许整张合约。

安全退出（到期风险、标的 hard stop、时间止损）不会被宽 spread 否决。权利金止损遇异常 spread 会产生 `ALERT + risk_exit`，要求 marketable-limit/人工接管，而不是静默 `HOLD`；无 bid 时只报警，不能假装成交。此系统不是高频交易，stop 不是成交保证。

## 策略顺序门槛

LongVol 不使用未经训练验证的加权总分：

1. 数据完整、新鲜且无未来信息；
2. 60 日/5 日 drawdown 与异常成交量构成 price-flow dislocation；
3. Yang–Zhang/downside 或 IV/skew/term/put-flow 至少一组独立确认 panic；
4. panic shock 后至少等待 2 个完整交易日；shock 后成交量必须衰减，且 ATR/实现波动率至少一项 compression，再要求 reversal/no-new-low；
5. 基本面必须由 PIT evidence 明确分类为 temporary dislocation 且 thesis `intact`；
6. 新鲜 short deterioration 否决；profile/short weakening/gamma/residual/IV surface 默认只记录；
7. 所有合约先满足 DTE、delta、OI、volume、spread 与新鲜报价约束；
8. 至少一张合约在 ask→conservative bid、IV crush、时间与双边费用后有正 target 净收益和负 stop 净收益；
9. 当前仓位模式、planned-stop risk 与 full-premium tail cap 均允许至少 1 张。

## 仓位与 Kelly

冷启动只计算冻结止损下的固定风险：

```text
loss_per_contract = (entry - option_stop) × multiplier + round_trip_fees
contracts = min(risk_budget / loss_per_contract,
                tail_budget / (entry × multiplier + entry_fee),
                1)
```

验证模式使用期权入场/目标/止损价格 `P/T/S` 和获批的保守胜率下界 `p`：

```text
gain = (T - P) × multiplier - fees
loss = (P - S) × multiplier + fees
b = gain / loss
full_kelly = p - (1 - p) / b
applied_risk_fraction = min(0.25 × full_kelly, 3%)
```

Kelly 分支仍同时受单笔 full-premium risk 8%、最多 2 张、组合 full-premium risk 20%、组合 planned risk 9%、最多 3 个持仓和实际 USD buying power 限制。但当前没有完成交易或获批 OOS 多状态收益分布，Kelly 必须保持禁用。USD 2,500 可以跑冷启动，但并非每个合约都能买到一张；超限应为 `WATCH`，不能放大预算。

## 退出优先级

从高到低：`EXPIRY_RISK`、`THESIS_BREAK`、`UNDERLYING_HARD_STOP`、`OPTION_PREMIUM_STOP`、`PROFIT_GIVEBACK`、`TIME_STOP`、`DTE_EXIT`、`IV_CRUSH`、`SHORT_PRESSURE_REACCELERATION`、`STRUCTURE_TARGET_REACHED`、`OPTION_TARGET_REACHED`。

开仓时永久冻结 entry ATR、标的/期权止损、目标、IV、planned risk、full-premium tail risk、到期日、multiplier 与最长持有期。标的硬止损不会因当前 ATR 上升而放宽，也不会被缺失 option bid 遮挡。多张仓位的结构目标 partial 只允许成交一次，之后 runner 由 option target 或风险规则退出。

## 审计与研究数据

`state/trading_log.sqlite3` 记录 runs、候选、每日特征、gates、仓位模式、账户快照、broker 订单、成交、仓位生命周期、退出决定与错误。本地 IV 保存在 `data/option_history.csv`，vendor regime 保存在 `data/option_regime.csv`，两者不可混合。

```bash
longvol export-log --log-db state/trading_log.sqlite3 \
  --table all --output-dir research_exports/2026-09-09
sqlite3 state/trading_log.sqlite3 \
  '.backup state/backups/trading_log_2026-09-09.sqlite3'
```

## Docker 为什么保留

Docker 只是可选的可重复运行环境，用于固定 Python/SDK 版本、隔离依赖和迁移；它不提供策略优势。OpenD 仍在宿主机，容器通过 `host.docker.internal` 连接。个人电脑直接使用 virtualenv 完全可以，初次排查 OpenD 权限时通常更简单。

完整的新规则、条件 Monte Carlo、PIT 新闻/DeepSeek 接入、部署、验收和参考资料见 [`docs/strategy_manual_v0.7.md`](docs/strategy_manual_v0.7.md)。设计证据与一致性推理见 [`docs/strategy_evidence_audit.md`](docs/strategy_evidence_audit.md)。
