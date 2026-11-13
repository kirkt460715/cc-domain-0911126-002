# 校舍资源配置

本项目使用 Python 3.11 及以上版本，仅依赖标准库，无需安装第三方包。数据文件均为本地合成记录，不含真实个人或机构资料。

estate 校验楼房、教室、学段、通勤和使用日历；allocation 进行整数分配并校验资源守恒；assessments 比较覆盖、通勤与投资选项；runs 维护情景修订、运行和发布。

共享楼宇容量会同时约束多个教室，教室不能同时分配给不兼容的学段。最低保障和精确搜索限额在结果中单独呈现。投资方案现有分析面向一个确定输入。

跨情景最小后悔决策通过 `assessments.robust` 与 `estate_cli.py robust` 使用。规划文件给出预算、项目（成本、依赖与互斥）和情景（仅调整需求学生数或楼房停用，不含概率）。投资组合只选定一次；每个情景内用原有兼容、通勤、共享楼宇上限和最低保障规则重新分配学生，不按情景另选投资。

排序依次为最大人数后悔最小、最坏情景服务人数最大、投资最小、全部情景分配成本之和最小、项目 ID 顺序。事后基准是同一预算下各情景的最优服务人数。无法满足最低保障的组合记为不可行，与数据冲突、搜索未完成分开报告。达到搜索上限时只给出候选和已证明的上下界，不标成最优。

```sh
python3 estate_cli.py robust --fixture fixtures/county.json --cases fixtures/planning_cases.json --save /tmp/decision
python3 estate_cli.py robust-matrix --bundle /tmp/decision
python3 estate_cli.py robust-verify --bundle /tmp/decision
python3 estate_cli.py robust-export --bundle /tmp/decision /tmp/decision-export
```

`robust-matrix` 展开组合乘情景的证据矩阵并列出最坏情景与各情景事后基准；`robust-verify` 重新读取时核对基础目录、项目及情景摘要并重算决策；`robust-export` 导出矩阵、基准与已校验的实际分配。

在项目根目录运行完整测试：

```sh
python3 -m unittest discover -s tests -v
```

编译检查：

```sh
python3 -m compileall -q .
```

实际演示：

```sh
python3 estate_cli.py demo --fixture fixtures/county.json
```

演示在临时目录内建立本地状态，结束后自动清理。测试、编译与演示在依赖已准备的环境内均不下载数据或软件。输入错误以非零退出码及错误对象报告。更多子命令可通过对应入口的 --help 查看。

容器环境可执行 `docker build -t local-analysis .`，再运行 `docker run --rm --network none local-analysis`。镜像固定基础内容摘要，构建后运行只访问本地文件。完整测试与演示也可直接使用已有 Python 环境。
