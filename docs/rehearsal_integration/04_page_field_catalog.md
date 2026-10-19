# 五页字段目录草案

配合[方案](01_solution.md)与[接口合同](02_integration_contract.md)阅读。本目录帮助业务大模型定位DTO/VO；仍需R0把枚举、必需产品、统计policy和现有主数据编码冻结成OpenAPI/JSON Schema，不能直接据此生成生产表。

最新用户需求增加管理列表与新建输入，详见[05补充](05_management_and_creation.md)。本目录“下边界”描述是后台数据产品合同，不再表示新建页面必须显示或要求用户填值；默认未知，不自动采用原型隐藏固定水位。当前R1授权合同/模板范围见[07](07_r1_authorized_work_order.md)。

通用规则：所有ID按字符串传输；显示名称不能作为关联主键；物理数值有限、缺失为NULL并带状态/原因；创建者/租户取服务端认证上下文。业务日期时间用 `yyyy-MM-dd HH:mm:ss` + `timeZone`，模型包另用带时区ISO8601。每个结果VO带result_set/scope/aggregation/frame/policy及来源质量。

## 1. 页面上下文与方案详情

| 字段组 | 字段 | 类型/单位 | 来源/要求 |
| --- | --- | --- | --- |
| 身份 | scenarioId、scenarioVersionId、runId、resultSetId、selectedScopeId | 字符串ID | 后端；未计算的run/result可null，不自动套旧版本 |
| 文本 | name、description、sourceDocumentPlanId | 字符串/ID | 草稿输入和旧资料目录引用；输出转义，富文本消毒 |
| 区域 | regions[{regionId,regionCode,regionName}] | 数组 | 基础资料ID及版本；多区并集scope独立定义 |
| 来源 | creationType、eventId、provenance、inputDatasetVersions | 枚举/ID数组 | 人工创建/推荐不等于模型已完成；暴露来源而不是写死 |
| 时间 | timeOriginLocal、timeZone、durationS、outputTimesS | 日期/时区/s/数组 | 冻结版本；时长显示小时仅换算，不由页面固定120 |
| 编制 | createdById/name、createdAt、updatedAt、versionNo | ID/字符串/日期/整数 | 后端；编制人和批准人分开 |
| 状态 | scenarioState、runState、publicationState、engineeringValidated | 分立枚举/布尔 | 不以一个completed覆盖全部状态 |
| 模型 | engineMode、engineVersion、inputHash、ruleSnapshotId | 枚举/字符串/摘要 | 后端/结果包，不能由展示页任意修改 |
| 收藏 | starred、userId | 布尔/身份 | 当前用户偏好，独立于方案物理版本 |

方案地图上的“预计启用时间、预计进洪量、进洪历时、设计蓄洪面积、设计蓄洪水位、相应蓄洪量、堤长、运用机遇”需要分开两类：

- 计划/计算指标：plannedActivationTime、predictedInflowVolumeM3、plannedInflowDurationS，来源是版本化预案或模型结果，标明planned/requested/computed。
- 工程设计属性：designAreaM2、designLevelM及verticalDatum、designStorageM3、embankmentLengthM、designRecurrenceDescription，引用工程资料快照，不能用实时仿真值覆盖设计值。

### 六个详情页签

| 页签 | 最低对象字段 | 主要值/单位 |
| --- | --- | --- |
| 上边界洪水 | boundaryId、sectionId、regionId、role、sourceVersion、temporalPolicy | timeS/dischargeM3s、峰值及峰值时刻；南支/北支属于不同对象 |
| 进洪过程 | structureId/boundaryId、direction、role、seriesId | timeS/dischargeM3s、累计正向进水volumeM3；输入与实际模型通量区分 |
| 泄洪过程 | structureId/boundaryId、direction、role、seriesId | timeS/dischargeM3s、累计泄出量；不能与带符号净交换量混用 |
| 水位预报 | stationId/pointId、mappingMethod/version、verticalDatum、role | timeS/waterLevelM；测站值、预测值、模型映射值不可互换 |
| 工程运用规则 | ruleId/version、conditions、actions、applicability、source/evidence | 比较符/测点/阈值/单位/基准、阶段和缺项；规则文本与执行事件分开 |
| 下边界 | boundaryId、quantity、unit、verticalDatum、temporalPolicy、source、applicationStatus | Q或绝对水位序列、方向/分配策略；不支持的引擎返回阻断 |

调度时间轴/地图标签共用事件VO：eventId、structureId、ruleId、timeS/timeLocal、action、reason、stage、sourceCategory、openingBefore/AfterRatio、dischargeBefore/AfterM3s、approvalRef、evidence、scriptId。图上“闸门全开”只表示其来源对应的计划或转录，不能因为入库就标实际已执行。

“扒口完成/宽度100m”等标签若需保存，属于结构措施描述：measureType、plannedTime、widthM、source和modelApplied。当前模型未批准/实现破堤通量，必须modelApplied=false或UNSUPPORTED，不能因页面支持标记就自动新增破堤物理。

## 2. 推演结果

| 页面值 | metricCode建议 | 规范单位 | 口径要求 |
| --- | --- | --- | --- |
| 淹没面积 | inundation_area | m2 | FRAME时刻面积；全程页明确peak_area或union_footprint，不用同一code猜含义 |
| 全程峰值面积 | peak_inundation_area | m2 | max_t A(t)，附peakTimeS |
| 全程受淹足迹 | inundation_footprint_area | m2 | 跨时刻空间并集 |
| 最大水深 | maximum_depth | m | 指定scope/aggregation，附时刻/位置和coverage |
| 影响村庄 | affected_village_count | count | 稳定村庄ID去重，冻结相交/影响定义 |
| 影响人口 | affected_population | person | 人口数据日期、精确/估计及方法、覆盖率；不可未算置0 |
| 淹没耕地 | affected_farmland_area | m2 | 资产相交结果，显示ha或亩须标清 |
| 淹没道路 | affected_road_length | m | 道路中心线/路面定义固定，相交段去重 |
| 淹没房屋 | affected_building_area | m2 | 建筑占地或楼面面积明确，不能复制淹没面积 |
| 洪水损失 | flood_loss | CNY | 金额精度、价格基准年、损失模型版本和复核状态；缺依据NULL |
| 峰值蓄水 | peak_storage_volume | m3 | 指定区域体积时序最大值，不等于累计进洪量 |

主结论VO：conclusionId、templateVersion、text、metricReferences、eventReferences、timeWindow、reviewStatus、editor/revision。每条结论的数字和时间都须有引用，可核查，不允许自由拼接演示句子。

分深度表：bandId、lowerM、upperM/null、lowerInclusive、upperInclusive、areaM2、farmlandM2、roadLengthM、population/status、coverage、scope/aggregation。区间边界由policy统一，不在每个前端组件重复定义。

到达时间表：assetId、assetType、name、townshipId/name、arrivalStatus、firstDetectedTimeS、lowerBoundS/upperBoundS、resolutionS、alreadyWetAtStart、affectedPopulation/status。未到达与缺覆盖分开；表格可显示“约第18小时”或区间，不用小时采样捏造17h51m。

三个结果地图产品分别为maximum_depth、arrival、wet_duration。各有variable/unit、有效掩膜、阈值/算法、分级图例和时间窗；没有适配/验收产品时禁用，不复用同一蓝色图假装切换。

## 3. 过程推演

| 控件/数据 | 字段 | 约束 |
| --- | --- | --- |
| 时间轴 | frameIndex、timeS、timeLocal、timeZone、availableProducts | 按manifest，非等距也可，首尾和缺帧明确 |
| 播放设置 | playbackSpeed、displayIntervalMs、playing | 前端偏好，不是模型时间步；不能修改权威统计 |
| 进洪曲线 | 同详情series VO | 当前时刻标线对应真实frame/time；保留单位与源角色 |
| 当前分洪流量 | diversion_discharge，m3/s | 是指定工程/断面瞬时量，不取全域速度推流量 |
| 累计分洪量 | cumulative_diversion_inflow，m3 | 积分同一进水序列；方向、区间/插值策略明确，不与净交换混用 |
| 当前影响卡片 | inundation_area、maximum_depth、affected_* | 均aggregation=FRAME、同frameIndex；人口/资产未算仍缺项 |
| 当前深度分级图 | depth_band_stat | 与同帧地图/面积闭合，零面积可为0 |
| 水深层 | depth帧产品 | mesh/topology、scope、mask、单位和frame一致 |
| 流场层 | velocity_x/y或经验证派生向量 | m/s、投影方向、干面策略和抽样规则明确；方向必须随坐标变换正确转换 |

“天气/渲染效果”不是雨量输入。页面的雨雪雾效果若存在，保存为displayPreference；不得暗中修改run的rainfall forcing。

## 4. 避险转移

| 对象 | 字段 | 来源/校验 |
| --- | --- | --- |
| 方案版本 | evacuationRevisionId、parentRevisionId、resultSetId、assetSnapshotIds、status、sourceType、review | 新增版本对象；人工/外部/算法建议区分，首期不宣称算法优化 |
| 转移单元 | unitId、villageCode/name、townshipCode/name、populationSnapshotDate、populationTotal、populationToTransfer | 引用基础资料，转移数与影响数/总人口的关系按审批规则检查 |
| 安置点 | shelterId/code/name、designCapacity、availableCapacity、occupiedCount、snapshotTime、geometryRef | 复用att_relc_base/安全区，设计容量不等于实时可用容量 |
| 安置分配 | allocationId、unitId、shelterId、plannedCount、transportMode、plannedDeparture/Arrival、routeRevisionId | 人员守恒、未分配人数、容量不足均显式输出；未知时间不猜 |
| 路线 | routeId/revision、geometryRef、CRS、start/endObjectId、lengthM、source、availabilityStatus | 独立版本；路网/桥梁/通行/水深阈值不足则NOT_ASSESSED，不标SAFE |
| 校核 | checkId、policyVersion、resultSet/frameWindow、hazards、missingInputs、warnings | 受淹路段、安置点风险、晚于洪水到达、无完整路由等分项，不给笼统“安全” |
| 执行信息 | executionRecordId、actualDeparture/Arrival、actualCount、reportSource、recordedBy | 独立后续业务记录；首期规划不自动生成执行数据 |

页面汇总：受影响村庄数、计划转移人数、目标安置点去重数、受淹道路长度/名称，均基于同一个避险版本和result_set；不能把全域影响人口、计划人数和已转移人数混在一张卡里。展示乡村层汇总即可，精确个人/家庭数据另走最小权限，不写入普通运行日志。

## 5. 三维展示

使用与二维页完全相同的metric、exposure、scope、frame和result_set，不另存一套“3D指标卡”。补充scene manifest：sceneId/version、terrainAssetId、buildingAssets、native/displayCRS、verticalDatum、waterProductRef、cameraPreset、assetQuality/coverage。

数据资产与渲染偏好分开：地形/建筑资源版本需要入库索引；相机朝向、天气效果、漫游速度等可为用户偏好，不作为仿真结果。静态PNG可作为明确标注的演示背景，不能变成“计算完成”的依据。

缺建筑/地形、垂直基准不一致、无有效水面产品、资源越权或过期时，显示具体不可用原因，不以旧场景顶替当前方案。暂不声明建筑内部淹水、桥下通行或垂向流速。

## 6. 编码前的字段交付检查

R0须为上述每个字段补齐：确切JSON名称、必填/可空、默认、枚举、范围、单位/基准、权限、统计算法/版本、数据库落点、来源/生成者和反例。复杂VO可复用统一Metric/Series/Layer/Asset结构，不每页复制物理字段；页面聚合VO负责组合，不再创造统计值。
