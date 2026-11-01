# 节点侧工具 schema 改从声明生成（2026-11-01）

分支 `fix/node-tool-schema-from-declaration`，基线 `154faf39`（#1296 合并后）。

## 背景：#1296 部署后仍然失败

#1296 让会话回合下发了 `cpms_site_search`，CD 也成功部署（线上 `1.0.0+154faf3917da`）。
但 41 上实测站点问题**仍然失败**，且失败点变了：

```
之前  agent_runs.reason = operator_failed              网关侧拒收
之后  agent_runs.reason = operator_node_failed         节点侧起不来
```

节点容器日志：

```
ExecConfigViolation: TOOLS: 有效工具定义与清单不一致：
  期望 [cpms_site_search/d=Search charging sites…/s=51432c970b4c928f…]
  实际 [cpms_site_search/d=Search charging sites…/s=ff1b2d8857c8e4a2…]
```

**名称和描述逐字相同，只有 schema 摘要不同。**

## 根因

`ff1b2d88…` 是那个**空参数 schema** 的摘要：

```bash
sha256('{"type":"object","properties":{},"required":[],"additionalProperties":false}')
  = ff1b2d8857c8e4a2cd87b566a798c5d1cd937fe8a13363e59a51231b4b328939
```

它来自 `CpmsReadTool` 的构造器：

```java
this.definition = DefaultToolDefinition.builder().name(name)
        .description(OperatorTools.description(name))
        .inputSchema(ControlledChargingStatusTool.SCHEMA)   // ← 状态工具的空参数常量
        .build();
```

**每个 CPMS 工具都用同一个空参数 schema**。而声明里 `cpms_site_search` 有
`model_inputs: ["name","address"]`，Java 侧 `Entry` 根本没读这个字段——所以节点即便想生成
正确 schema 也无从下手。

`verifyTools` 比的是 `name/d=描述/s=schema的sha256`，于是判漂移、整轮拒绝启动。

## 修复

1. `OperatorTools.Entry` 增加 `modelInputs`，解析时读 `model_inputs`（缺省为空数组）。
2. 新增 `OperatorTools.inputSchema(name)`，按与 Python
   `operator_tools.OperatorTool.input_schema()` **逐字节对应**的方式拼接。
3. `CpmsReadTool` 改用 `OperatorTools.inputSchema(name)`。

两侧拼的是同一个字符串，因为 `verifyTools` 比的就是它的 sha256：**差一个空格就判漂移**。
两处注释都写明了「改动要两边同改」。

## 验证

新增验收件 `tools/experiments/m2_close/m3node/ToolSchemaParity.java`，并**接进
`run.sh` 的 `schema_phase`**（不接进去的话它只被编译、从不执行——复核指出过这一点）。
期望值由 Python 侧同一份声明算出，Java 侧对**实际构造出的 `CpmsReadTool`** 自算一遍比对。
无参数时打印 `STATUS=FAIL` 退出 2，避免"忘了传参"看起来像通过。

```
cpms_order_detail      ff1b2d88…  PASS
cpms_charging_samples  ff1b2d88…  PASS
cpms_site_search       51432c97…  PASS
TOOL_SCHEMA_PARITY PASS assertions=3
```

**变异验证**：把 `CpmsReadTool` 改回 `ControlledChargingStatusTool.SCHEMA` 重新编译后：

```
FAIL cpms_site_search actual=ff1b2d88… expected=51432c97…
TOOL_SCHEMA_PARITY FAIL assertions=3
```

### 第一版验收是假通过

第一版 `ToolSchemaParity` 读的是 `OperatorTools.inputSchema(name)`——那只是声明侧的字符串。
构造器不接它时两者照样一致，所以**回退修复后仍然全部 PASS**。

这与 `verifyTools` 是同一条教训：**判据必须读被测路径真正使用的那份值**。
改读 `new CpmsReadTool(name, …).getToolDefinition().inputSchema()` 之后变异才变红。

## 未做

没有重新构建节点镜像、没有部署。节点是 Docker 容器（`aiops-runtime-node`），
新 jar 需要按既有节点发版流程构建并替换镜像。
