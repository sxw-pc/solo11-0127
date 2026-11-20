# 面单印发核台

物流站点把本地面单软件的打印作业、主管重印授权和承运回执采入同一 SQLite 证据账。印制判断纸号、模板与回执；重印裁决原份作废和新份生效；交接核对面单、重量、承运人，并独立结算整批交接。原始桌面导出字节及 SHA-256 持久化，JSON 报告列来源、实体、问题和每次业务决定。

四个入口分别是 `print-import`、`reprint-request`、`handoff-evaluate` 和 `handoff-settle`，均接收 `--db ledger.db export.json`。导出 JSON 顶层含 `version` 和 `rows`。`demo` 会在临时目录生成完整正常链路；测试还覆盖纸号冲突、缺打印回执、授权过期、重量不符及部分失败结算。`service-confirm` 与 `delivery-import` 同样接收 `--db ledger.db export.json`。

打印资源是独立的排程决定：`printer-plan` 读取桌面设备与纸库存快照，为份次分配支持模板且有纸、有空闲容量的打印机。`printer-cancel` 必须提供无纸张输出的操作员证据才释放预留；已消耗的纸张不会因重试回到库存。印制及授权补印会读取排程结果，资源不足时等待。

承运方接收后仍可提出实物争议：`carrier-claim` 核对实际看到的面单份次，证实不符时把包裹置于召回中、把已结算批次重开；`recall-resolve` 依实物扫描证据结束追索。争议被驳回时原交接结论不变，重放旧结算导出也不能重新封账。无有效原份、占用中的新份 ID 或已用纸号不能获批重印；召回中的包裹不能再次交接。相关来源、预留、索赔与召回实体均写入台账；演示只用本地虚构文件。

## 末端实收核证

交给承运员不等于收件人已收到。`service-confirm` 导入寄件时固定的服务与收件人确认（`safe_place` 安全位置投放，或 `face_to_face` 当面核一次性码），`delivery-import` 逐次导入离线投递尝试：包裹、有效面单份、承运员、封装编号、尝试时刻、地点、收件签认或门口原图摘要。每条尝试按事件时刻独立留存，不因上传顺序被后一条覆盖。

判定规则：`safe_place` 需地点与授权地址一致、可信定位与门口原图同时具备；`face_to_face` 需收件签认与一次性码，同一批门口照片和定位只能证明有人到过门口。GPS 数字或图片摘要单独不构成交付；一次性码不得证明两个包裹；作废或重印前的面单不能证明新包裹；交接状态、承运员、封签、当前有效面单必须与证据一致。授权缺失、尝试早于当次交接周期（重印后新交接）或交接被争议时，证明不采信。拒收、空跑、退回即使晚到也按事件时刻参与结论，成功之后的拒收会把包裹退回待追投。

已结算的承运交接仍是历史事实；末端结论只写新的 `delivery_outcome` / `delivery_assessment` / `delivery_cycle` 实体与决策记录，并进入后续 `handoff-settle` 批次对账（`settlement_item` 携带当前履约状态）。`report` 输出 `fulfilment`（逐包裹的服务授权、每次尝试的实物证据、冲突原因与来源哈希）、`fulfilled` 与 `awaiting_redelivery`。新入口为 `service-confirm` 与 `delivery-import`。

运行：`python -m pip install . --no-deps --no-build-isolation`，`python -m labeldispatch demo`，`python -m labeldispatch --help`，`python -m pytest -q`。所有演示和测试使用虚构本地文件，不访问网络。
