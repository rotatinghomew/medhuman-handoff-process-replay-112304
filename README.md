# 见习回执的流程变体诊断与交接改版验效

临床沟通见习回执的多来源日志、留存窗口和交接版本记录。 数据不含真实学生、患者或就诊记录。数据表在 `data/`，业务字段和交换约定见 [资料口径](data-contract.md)。

在 Python 3.11 下可直接清点资料和检查表结构。统计环境使用 `requirements.txt` 中锁定的版本，可在虚拟环境中一次安装后离线使用。

```sh
python3 -m pip install -r requirements.txt
python3 inspect_bundle.py data
python3 -m unittest discover -s tests -v
```

资料检查入口只读，输出每张表的记录数，读取到不符合清单的表时退出。空白字段按资料口径解释。

## 流程诊断与交接改版回放

`src/` 实现按资料口径的变体诊断与规则回放，`run_analysis.py` 是只读分析入口，
完整方法与结论见 [analysis/METHOD.md](analysis/METHOD.md)：

```sh
python3 -I run_analysis.py data --out analysis/report.json
python3 -I -m unittest discover -s tests -v
```

分析纪律：副本（`copy`）不构成接手；补录（`supersedes`）归并且在其 `arrived`
前不参与值班判断；等待以 `arrived` 为主口径，时钟区间仅用于检验先后能否唯一
确定，区间重叠不作单环节归因；退回、到观察期无回执、尚未到交件时间三类结果
严格分开；回放只用各时点已到达的事实，缺件不虚构工作量，并区分历史推演与
pilot_log 中已观察的试行结果。

