# 资源股滞后套利 · 自动监控

基于原型 `资源股滞后套利监控.dc.html` 的落地实现:每日收盘后自动抓取数据、
计算六道闸门、生成快照 JSON,仪表盘页面按原型样式展示,可回看历史快照。

## 数据源方案(核心问题)

| 闸门 | 需要的数据 | 数据源 | akshare 接口 | 状态 |
|------|-----------|--------|--------------|------|
| 总开关 | 商品指数 vs MA200 | 中证商品指数 100001.CCI(约4年日线,替代南华指数) | `futures_index_ccidx` | ✅ 已验证 |
| ① 商品启动 | 主力合约 30 日涨幅 | 新浪期货主力连续(LC0/SA0/CU0/AL0/ZN0) | `futures_main_sina` | ✅ 已验证 |
| ③ 现货紧张 | 近月/远月价差(Back) | 生意社期现表(54 个期货品种) | `futures_spot_price_daily` | ✅ 已验证 |
| ④ 库存去化 | 交易所库存周度变化 | 东方财富期货库存(日度) | `futures_inventory_em` | ✅ 已验证 |
| ⑤ 量仓齐升 | 主力合约收盘价+持仓量 | 同① (日线自带持仓量) | `futures_main_sina` | ✅ 已验证 |
| ②⑥ 股价滞后/个股健康 | A股前复权日线 | 东方财富 | `stock_zh_a_hist` | ✅ 已验证(需限速) |

**没解决的一块:无期货品种的现货价(稀土·氧化镨钕 / 黄磷 / MDI)**

- 生意社(100ppi.com,含英文站 sunsirs.com)有这三个品种的日度基准价,但网页有
  反爬(普通请求只返回挡板页),需要 Playwright 无头浏览器渲染后解析,或购买其
  数据服务。
- 稀土可用「中国稀土行业协会」日度稀土价格指数页面;有色可看 SMM(上海有色网,
  核心价格需付费)。
- 当前实现:这三个品种在 `config.json` 里用 `manual_c30` 手工维护 30 日涨幅
  (为 `null` 时品种标记"缺数据",不产生信号,不会误报)。
- 后续升级:写一个 Playwright 抓取器每日更新这三个价格,即可全自动。

其他注意:

- **南华指数接口已从 akshare 移除**,南华官网 JSON 也已 404,故总开关改用中证
  商品期货指数(中证商品指数公司官网,免费,历史约 4 年,够算 MA200)。
- **东财 A股接口有限速**:连续快速请求约 15 次后会断连,`monitor.py` 已做
  1.5s 间隔 + 指数退避重试。全池 35 只股票一轮约 3~5 分钟。
- 备选数据源:Tushare Pro(积分制,期货+股票全覆盖、接口更稳,适合以后换)、
  Baostock(仅股票)。

## 使用

```bash
# 首次
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install akshare

# 每日收盘后跑一次(期货结算后, 建议 17:30)
.venv/bin/python monitor.py

# 看盘面
python3 -m http.server 8600
# 打开 http://localhost:8600
```

### 定时任务(macOS cron 示例,本地跑)

```
30 17 * * 1-5 cd /Users/mac/Projects/github/neohoy/linghua && .venv/bin/python monitor.py >> monitor.log 2>&1
```

### 自动化部署:GitHub Actions + Pages(推荐)

不用自己开机器。`.github/workflows/monitor.yml` 已配置好:

- **定时**:每个工作日 17:30 北京时间(期货结算 + A股收盘后)自动跑 `monitor.py`,
  把新快照 commit 回仓库的 `data/` 目录
- **手动/回填**:在 GitHub 仓库的 Actions 页面手动触发该 workflow,可在
  `dates` 输入框填 `2026-08-18 2026-08-19` 这样的空格分隔日期来回填历史快照
- **展示**:仓库 Settings → Pages → Source 选 `Deploy from a branch` →
  `main` / `(root)`,`index.html` 会直接读同仓库的 `data/*.json`,每次
  workflow commit 后 Pages 自动重新发布,几十秒内生效

依赖版本锁在 [requirements.txt](requirements.txt) 里(CI 用,和本地 `.venv` 保持一致)。

**已知风险**:GitHub Actions 的 runner 是海外 IP,东方财富的股价接口对国内 IP
都会限频,海外 IP 更没把握——`monitor.py` 已有腾讯行情自动降级(`_em_dead` 计数
连续失败即切换),多数情况下能兜住,但建议上线后观察几天 Actions 日志确认稳定。
如果长期不稳定,再迁到国内轻量云服务器(阿里云/腾讯云,约 ¥30-60/月)按上面
macOS cron 的思路配 systemd timer。

### 推送通知

`config.json` → `notify` 填入 Bark URL 或 Server酱 key,当日有信号触发时推送。

## 规则口径(monitor.py 实现)

- 30 日 = 30 个自然日(取该日前最近交易日收盘为基准),阈值都在 `config.json → rules` 可调
- ③ Back:近月合约价 − 主力(远月)合约价 > 0;若主力即近月,退化为 现货 − 主力
- ④ 库存:按周五对齐重采样,最近连续下降周数 ≥ 2
- ⑤ 量仓:最近 5 个交易日中「收涨且增仓」天数 ≥ 3
- 六道闸门每日全量计算并展示,与总开关状态无关;总开关只决定是否产生信号
- 信号 = 总开关开 && 品种级①③④⑤全过(现货品种③⑤免检) && 个股级②⑥全过;
  任一闸门缺数据则该标的不产生信号(宁缺勿错)
- 总开关关闭但闸门全过的标的,仪表盘上显示灰点(开关一开即触发)

## 文件

- [config.json](config.json) — 品种池、规则阈值、通知配置
- [monitor.py](monitor.py) — 抓数 + 六道闸门计算,输出 `data/<date>.json`(支持
  `python monitor.py 2026-08-18 2026-08-19` 按日期回填)
- [index.html](index.html) — 仪表盘(读 `data/`,按原型视觉复刻,点击闸门卡片展开明细)
- [.github/workflows/monitor.yml](.github/workflows/monitor.yml) — 定时抓数 + 回填 workflow
- [requirements.txt](requirements.txt) — CI 依赖锁定
- `data/` — 每日快照(`index.json` 为日期索引,保留最近 60 个)
