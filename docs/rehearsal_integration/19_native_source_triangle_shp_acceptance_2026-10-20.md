# 原生源项三角SHP独立验收（2026-10-20）

批次NATIVE-SOURCE-TRIANGLE-SHP-20261020，基线b2f40c3，工作单18。真实实施聊天01a13ecc-73db-7b40-ab59-622559d236f3，实际turn01a15747-8e5e-7d80-b14a-e8a840c6e133，显式gpt-6-luna/max；本窗口独立验收，未并改生产。

## 独立参考

新参考真实使用既有三种runner/strict reader：2面、3时刻0/0.025/0.05s、EPSG4548大坐标小数顶点，IDs9007199254740993/9223372036854775806，初V1.3600000001490113m³，面面积4.035000000210712/1.495000000857981m²。Manning/combined有真实nonzero_manning_effect，rain/combined有真实非零雨效应；雨强0.02m/s，末V1.36553000015008m³，Manning体积保持到1e-12。base-none结果从未生成。

独立安全8面/active15m²案例初V3.61→3.6115m³，接受雨.0015m³，排除潜在未模拟雨.0001m³；真正干域零雨保存h全0；同一base的Manning/rain零效应保存态相同但native family不同。三动态子窗口逐面峰值包络体积1.37467/1.37826/1.37820显著大于同步最大1.36/1.36553/1.36553，不允许混用。仅本批小型合成，无真实DEM/DB/工程运行。

## 首轮独立预审

冻结40项实际38 passed/2 failed/0skip in82.56s，exit1，70模块来自workspace。通过项包含三family真实FRAME/子窗口MAX4490、零/干/安全、完整来源图/逐帧预算/同步体积、无base结果/错误fallback拒绝、输入类型/来源及原生水量篡改、DBF/同面积错顶点重哈希、manifest预算/graph/糙率自一致伪造及postpublish错误退役。

两失败不是生产缺陷：反例向native新增未声明的全局case_id/solver_version属性；正式协议中的对应身份保存在contract_metadata_json。先只读核实原生属性/现有reader后，将反例改为修改正式JSON身份字段，拒绝断言不变，原始文件保存在work/acceptance_native_source_shp_20261020/initial_identity_attribute_probe.txt。更正后4项真实身份篡改定向复验4 passed in6.27s、exit0、0skip；不能把初始2项误报成reader漏洞或把4项累计成42个新测试。新独立文件SHA38b2b4dba0ca1618787095e7a0f02ff0901463497d1d5a3b3c3d1153572bca81，夹具SHA805cbe7a26d3b202bcaf6dc78b0701cdb1b7baf043f8e241ab23069f2338e398。上游数值/reader/contract/security模块SHA未变。

当前只是活动预审，待实施final后锁定确切源码，原样完整40项与必要旧兼容、隔离闭包及wheel门禁后才能提交推送；17旧68不重写，不把既有结果重复当本批新增验收。

## 正式交付与最终工作区复验

实际实施完成turn=01a15747-8e5e-7d80-b14a-e8a840c6e133 / cursor58，未把恢复授权turn当作实施身份。实施方新2项集成、1项独立wheel及v1少量兼容真实通过，未代替本窗口验收。

锁定确切适配器及两个schema后，本窗口实际40新独立+68受影响公共导出/安全区兼容同次108 passed in97.44s，exit0、0skip，75模块均来自workspace；仅旧故意NaN拒绝案例产生一条Shapely警告。执行前后adapter/schema SHA一致；数值核心、三种reader/contract、安全几何上游10文件SHA全部未变，独立反例/夹具未修改。此前两个非正式属性反例纠正已如实保留历史，正式拒绝标准不变。工作区功能通过，接着基于b2f40c3最小依赖隔离候选及wheel，不宣称已发布。

## 最小发布候选

基于b2f40c3的31路径候选只更新适配器、独立v2 schema/正式测试、纯合成输入和说明/独立回归，不采用当前__init__/pyproject的其他WIP。实际126 passed、2 deselected、0skip in159.31s，两个wheel检查各在全新进程1 passed in2.01s、1 passed in7.95s，均exit0，总128项有实际通过证据。38个项目模块全部来自candidate，editable/live回退禁用；仅旧故意NaN拒绝产生一条Shapely警告。独立index diff-check通过。数值核心/reader/contract/security源码保持b2f40c3既有版本，未重复不受影响旧数值大集合。最终wheel安装与CLI门禁随后执行，尚未提交新批次。

## 最终wheel实际安装

最终wheel SHA256=c845aac4f54ef75d06e9da3eba60ffe3c1dde4e5feb586170ecd7b2ce275c258，独立安装后CLI schema/schema-v2/export/validate实际通过，47个package文件与候选/安装包逐字节一致，32项目模块均来自wheel。三family各2面/0.05s及安全combined8面/1s真实运行和严格reader得到原生数据；base-none结果全部不存在，source原字节未变，face ID/实际三角geometry/预算与native一致。Manning末V1.360000000149012，rain/combined末V1.36553000015008，安全combined末V3.6115，排除未模拟雨.0001。can_publish/engineering_validated均false。

108工作区、128候选分组及wheel门禁均通过。另只依据现有YAML和已批准能力整理20工程输入缺项，不打开真实DEM/业务CSV、不运行工程，不选择新的物理默认值。可按既有持续授权发布本批最小白名单，不纳入其他WIP。
