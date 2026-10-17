# examples · 示例产物

这里放**真实跑出来的小样本**（不含大文件），让你在看代码之前先了解数据形态。

| 文件 | 来自哪次运行 | 是什么 |
| --- | --- | --- |
| `hotlist.md` | `fetch_hotlist.py --top 10`（免登录） | 各平台热榜样例 |
| `collected.json` | 主题「阿里千问」B站 Top5 | 六平台采集的原始条目结构 |
| `references.json` | 同上 | 蒸馏后的参考条目（含 content_source 分级） |
| `quotes.json` | 同上 | 金句 + 出处结构 |
| `verification.json` | 同上 | 交叉验证结果 + todo_verify |

> 完整的 HTML 报告因体积原因不入库，形态见 README 截图。
