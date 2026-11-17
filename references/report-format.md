# 报告 JSON 与渲染约定

虚构且可运行的排版示例见 `examples/report-demo.json`。所有文字由 Agent 在阅读证据后写入，渲染器只排版与检查结构，不负责理解语音、声纹识别或证明事实，也不会自行联网。

必填顶层字段：

- project_id、timezone、window.start/end_exclusive：ISO8601带时区。
- title、subtitle、date_label、summary、badge。
- privacy_reviewed：Agent审阅脱敏后设true，不能据此宣称机器已自动消除所有敏感信息。
- retrieval：queried_at、status（complete/partial/no_data/metadata_only）、scope、counts、read_counts。
- metrics：value短文字与label；不使用活动得分、虚构工时、无依据可信百分比。
- coverage_note、limitations[]：必须写无转写、时区、说话人/媒体与读取限制。
- events[]：start/end、display_time、title、description、category、speaker、confidence、status、evidence_ids[]。本人活动另需speaker_basis说明用户已确认的说话人映射/直接证据。按开始时间排序，宽时段注明近似，不能用片段开始秒冒充整个活动起始。
- highlights[]、ideas[]、todos[]：title、description、evidence_ids[]；每条自行写明归属、提议/承诺/完成、负责人/期限/是否推断。
- evidence[]：id（E01等短标签）、source_table、source_id（完整源ID）、recording_id（如有）、start/end、paraphrase（脱敏转述，非引文）。

推荐图像包含：英文 report 小标题、日期范围、主标题、摘要、活动概览卡、纵向时间线、重点事件、创意与决策、待办、证据与置信限制。不要复制群聊发言榜。证据短标签放图上，完整ID保留在 JSON/Markdown。

渲染依赖 Python3.10+ 和 Pillow；使用系统中文字体或显式本地字体路径，不捆绑商业字体、不走网络字体、不发送隐私到外部生图服务。默认1080px宽，高度自动扩展。PNG保存完整长图；可选PDF为同一图片嵌入单长页，非可编辑排版/分页文档。超长周报优先按天输出，再加一张周汇总，保留每图的日期与证据。

Agent 内部调用示例（用户只需自然语言）：

```sh
python3 scripts/report_tools.py window --period yesterday --tz Asia/Shanghai --now 2026-11-16T18:00:00-08:00
python3 scripts/report_tools.py sql --kind transcripts --start 2026-11-13T16:00:00Z --end 2026-11-14T16:00:00Z --single-user --limit 200 --offset 0
python3 scripts/render_report.py --input examples/report-demo.json --output report.png
# 正式报告必须传与实际授权连接一致的 --project-id，示例不连接数据库。
```

最终交付：中文结论（不重复整张图）、可打开PNG、证据JSON、Markdown摘要；需要时PDF。生成后视觉检查全文不能替代事实核对。
