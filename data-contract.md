# 资料口径

分钟自教学周期第 0 日当地零点起算，开始和结束给可能发生区间。状态 `copy` 不履行接手，`returned` 结束本次未经确认的交件，`supersedes` 是同一业务动作补录的指向关系。原事件的 `arrived` 是记录可用于值班判断的分钟。进度截止按每位学生的时间，教学场次共用同一 `group` 时，共用动作在一场工时统计中只计一次。
流程 `either` 表示任选一个列出的接手人，`parallel` 内的动作互不规定顺序但在有前置约定时是独立义务。以简写动作名识别真实行为，重叠的时钟范围不表示两动作同时完成。回放时教师容量是给出的同一窗口内能够开始处理的最大值，接手只能交给有资格且未超出容量的人。版本按公示生效分钟和 `locked` 约定约束在途实例。

数据表使用 UTF-8 CSV，第一行是字段名。同一业务号的引用指数据字典约定的原始对象；空白表示来源未提供，集合字段由逗号分隔后整体按 CSV 转义。
- `placement.csv` 的字段为 `id`、`group`、`site`、`version`、`teacher`、`eligible_min`、`due_min`、`state`。
- `log.csv` 的字段为 `id`、`placement`、`action`、`actor`、`earliest`、`latest`、`arrived`、`kind`、`supersedes`。
- `observability.csv` 的字段为 `placement`、`action`、`from_min`、`to_min`、`state`。
- `workflow.csv` 的字段为 `version`、`site_set`、`from_min`、`scope_required`、`either`、`parallel`、`before_register`。
- `coverage.csv` 的字段为 `person`、`from_min`、`to_min`、`role`、`skills`、`capacity`。
- `launch.csv` 的字段为 `site`、`version`、`proposed_at`、`takes_effect`、`trial_to`、`state`。
- `notice.csv` 的字段为 `placement`、`notice_at`、`version`、`locked`、`promised_route`。
- `pilot_log.csv` 的字段为 `id`、`placement`、`action`、`actor`、`earliest`、`latest`、`arrived`、`kind`、`supersedes`。
