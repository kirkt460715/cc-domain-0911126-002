# 校舍资源配置

本项目使用 Python 3.11 及以上版本，仅依赖标准库，无需安装第三方包。数据文件均为本地合成记录，不含真实个人或机构资料。

estate 校验楼房、教室、学段、通勤和使用日历；allocation 进行整数分配并校验资源守恒；assessments 比较覆盖、通勤与投资选项；runs 维护情景修订、运行和发布。

共享楼宇容量会同时约束多个教室，教室不能同时分配给不兼容的学段。最低保障和精确搜索限额在结果中单独呈现。投资方案现有分析面向一个确定输入。

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
