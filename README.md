# 面单印发核台

物流站点把本地面单软件的打印作业、主管重印授权和承运回执采入同一 SQLite 证据账。印制判断纸号、模板与回执；重印裁决原份作废和新份生效；交接核对面单、重量、承运人，并独立结算整批交接。原始桌面导出字节及 SHA-256 持久化，JSON 报告列来源、实体、问题和每次业务决定。

四个入口分别是 `print-import`、`reprint-request`、`handoff-evaluate` 和 `handoff-settle`，均接收 `--db ledger.db export.json`。导出 JSON 顶层含 `version` 和 `rows`。`demo` 会在临时目录生成完整正常链路；测试还覆盖纸号冲突、缺打印回执、授权过期、重量不符及部分失败结算。

打印资源是独立的排程决定：`printer-plan` 读取桌面设备与纸库存快照，为份次分配支持模板且有纸、有空闲容量的打印机。`printer-cancel` 必须提供无纸张输出的操作员证据才释放预留；已消耗的纸张不会因重试回到库存。印制及授权补印会读取排程结果，资源不足时等待。

承运方接收后仍可提出实物争议：`carrier-claim` 核对实际看到的面单份次，证实不符时把包裹置于召回中、把已结算批次重开；`recall-resolve` 依实物扫描证据结束追索。争议被驳回时原交接结论不变。相关来源、预留、索赔与召回实体均写入台账；演示只用本地虚构文件。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m labeldispatch demo`，`python -m labeldispatch --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。
