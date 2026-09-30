/** cd-watch 的判定分支自检（不联网）：用第 407 号票记录的真实形态驱动。 */
import { assess, classifyRun, overdueUnfinished, straightFailures } from "./deploy-state.mjs";

const now = new Date("2026-09-30T15:30:00Z");
const cases = [];

// 1. 真实：run 36678842333（58dc271）—— waiting + 有 pending deployment
cases.push([
  "等批准超阈值 ⇒ stuck",
  classifyRun({ status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: 1, pendingCount: 1 }, { now }).kind,
  "stuck",
]);

// 2. 票里记录的**死锁**形态：run 存在、waiting、jobs=0、已超时
//    （曾判成 indeterminate ⇒ assess 报正常 ⇒ 把本票要修的静默又做了一遍）
cases.push([
  "超时仍 jobs=0 ⇒ deadlocked",
  classifyRun({ status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: 0, pendingCount: 0 }, { now }).kind,
  "deadlocked",
]);

// 2b. 详情**取不到**（null）⇒ 未知，不是死锁、更不是正常
cases.push([
  "详情取不到 ⇒ indeterminate",
  classifyRun({ status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: null, pendingCount: null }, { now }).kind,
  "indeterminate",
]);
cases.push([
  "indeterminate ⇒ 报异常（未知不是正常）",
  assess({
    runs: [{ status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: null, pendingCount: null }],
    unfinishedRuns: [{ status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: null, pendingCount: null }],
    fetchedOk: true,
  }).ok,
  false,
]);

// 3. 有作业、无人等批准、**已获批** ⇒ 在等并发锁。这一段被两个方向各纠过一次：
//      · 判 `deadlocked` ⇒ 假阳性（合法排队被说成"卡死了"）；
//      · 改成"不看时长" ⇒ 另一个洞（运行器离线时会永远显示健康）。
//    ⇒ 结论是「**要判，但不是死锁**」：给它一个说得出成因的类别。
cases.push([
  "已获批却在阈值内 ⇒ in-flight（合法排队）",
  classifyRun({ status: "waiting", createdAt: "2026-09-30T15:25:00Z", totalCount: 1, pendingCount: 0 }, { now }).kind,
  "in-flight",
]);
cases.push([
  "已获批却超阈值 ⇒ queued-too-long（运行器可能离线）",
  classifyRun({ status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: 1, pendingCount: 0 }, { now }).kind,
  "queued-too-long",
]);

// 4. 刚创建 5 分钟 ⇒ 不报警
cases.push([
  "阈值内 ⇒ in-flight",
  classifyRun(
    { status: "waiting", createdAt: "2026-09-30T15:25:00Z", totalCount: 1, pendingCount: 1 },
    { now },
  ).kind,
  "in-flight",
]);

// 5. 连续 6 次 cancelled（票里那段的形态）：数到第一个 success 为止，只在已完成的里数
cases.push([
  "连续失败计数（跳过未完成的）",
  straightFailures([
    { status: "waiting", conclusion: null },
    { status: "completed", conclusion: "cancelled" },
    { status: "completed", conclusion: "cancelled" },
    { status: "completed", conclusion: "success" },
  ]),
  2,
]);

// 6. 取数失败 ⇒ 不报正常
cases.push(["取数失败 ⇒ ok=false", assess({ runs: [], fetchedOk: false }).ok, false]);

// 6b. **空列表**也 ⇒ 不报正常。「一次都没触发过」与「一直正常」在告警面上是同一种安静。
cases.push(["取不到任何 run ⇒ ok=false", assess({ runs: [], fetchedOk: true }).ok, false]);

// 6c. 🔴 **卡住的旧 run 被新 run 挤出视野 ⇒ 故障"自愈"**（上线当天实测撞到：
//     一个等了 84 分钟的在等批准，而 assess 说「正常」，因为 runs[0] 是那个刚 push 的）。
cases.push([
  "旧 run 卡住、新 run 在后 ⇒ 仍要报警",
  assess({
    runs: [
      { status: "pending", createdAt: "2026-09-30T15:28:00Z", totalCount: 1, pendingCount: 1 },
      { status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: 1, pendingCount: 1 },
    ],
    unfinishedRuns: [
      { status: "pending", createdAt: "2026-09-30T15:28:00Z", totalCount: 1, pendingCount: 1 },
      { status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: 1, pendingCount: 1 },
    ],
    fetchedOk: true,
    now,
  }).ok,
  false,
]);

// 6d. **但排队不是卡住**：并发锁只保证串行，等着接替前一个的 run 本来就该等。
cases.push([
  "正常排队（最早那个没超期）⇒ 不报警",
  assess({
    runs: [
      { status: "pending", createdAt: "2026-09-30T15:28:00Z", totalCount: 1, pendingCount: 1 },
      { status: "waiting", createdAt: "2026-09-30T15:10:00Z", totalCount: 1, pendingCount: 1 },
    ],
    unfinishedRuns: [
      { status: "pending", createdAt: "2026-09-30T15:28:00Z", totalCount: 1, pendingCount: 1 },
      { status: "waiting", createdAt: "2026-09-30T15:10:00Z", totalCount: 1, pendingCount: 1 },
    ],
    fetchedOk: true,
    now,
  }).ok,
  true,
]);

// 7. 连续 3 次失败 ⇒ 报警。**没有未完成的 run 是正常状态**（`[]`），
//    与「未完成集合取不到」（`null`）不同 —— 两者混同会让每次正常运行都报 unavailable。
const many = assess({
  runs: [
    { status: "completed", conclusion: "cancelled" },
    { status: "completed", conclusion: "cancelled" },
    { status: "completed", conclusion: "cancelled" },
  ],
  unfinishedRuns: [],
  fetchedOk: true,
  straightFailures: 3,
});
cases.push(["连续 3 次失败 ⇒ 报警", many.ok, false]);
cases.push(["告警种类正确", many.alarms.map((a) => a.kind).join(","), "straight-failures"]);

// 7b. 🔴 **未完成集合取不到 ⇒ 未知**（不能用最近列表代替：那个窗口不完整，
//     而「jobs=0 ⇒ 死锁」这条判据恰恰依赖完整性）。
cases.push([
  "未完成集合取不到 ⇒ 报 unavailable",
  assess({
    runs: [{ status: "completed", conclusion: "success" }],
    unfinishedRuns: null,
    fetchedOk: true,
  }).alarms.map((a) => a.kind).join(","),
  "unavailable",
]);

// 7c. **一条 indeterminate 的旧 run 不得挡住后面一条确凿的 stuck** ——
//     这曾经是「只看最早那条」的另一个投影（评审指出）。
cases.push([
  "旧 run 未知、新 run 确凿卡住 ⇒ 两者都报",
  assess({
    runs: [
      { status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: 1, pendingCount: 1 },
    ],
    unfinishedRuns: [
      { status: "waiting", createdAt: "2026-09-30T06:33:29Z", totalCount: 1, pendingCount: 1 },
      { status: "waiting", createdAt: "2026-09-30T08:00:00Z", totalCount: null, pendingCount: null },
    ],
    fetchedOk: true,
    now,
  }).alarms.map((a) => a.kind).sort().join(","),
  "indeterminate,stuck",
]);

// 8. 🔴 **枚举 `overdueUnfinished` 到底把哪些判成要报警** —— 断言**它返回的类别**，
//    不是 `assess().ok`。上一轮我断言 `ok`，而「两条 run 组成的队列恰好仍为 true」
//    让一个真 bug（把普通排队当异常）**通过了用例**。
//
//    ⚠️ 这一条还犯过一次更蠢的错：曾经写成把同一个常量同时当实测值与期望值
//    （`["stuck",...]` vs 同样的字面量），**那个断言永远不会失败**（评审指出）。
//    现在它真的喂进三类输入、读回类别。
const alarmingKinds = [
  ["等批准超阈值", { status: "waiting", totalCount: 1, pendingCount: 1, createdAt: "2026-09-30T06:33:29Z" }, "stuck"],
  ["jobs=0 超阈值", { status: "waiting", totalCount: 0, pendingCount: 0, createdAt: "2026-09-30T06:33:29Z" }, "deadlocked"],
  [
    "详情取不到",
    { status: "waiting", totalCount: null, pendingCount: null, createdAt: "2026-09-30T06:33:29Z" },
    "indeterminate",
  ],
  [
    "已获批却开不了工",
    { status: "waiting", totalCount: 1, pendingCount: 0, createdAt: "2026-09-30T06:33:29Z" },
    "queued-too-long",
  ],
];
const gotKinds = alarmingKinds.map(([, run]) => {
  const overdue = overdueUnfinished([run], { now });
  return overdue.length === 1 ? overdue[0].verdict.kind : `(none:${overdue.length})`;
});
cases.push(["算卡住的那 4 类（真喂进去再读回）", gotKinds.join(","), "stuck,deadlocked,indeterminate,queued-too-long"]);

// 8b. **不算卡住的那些必须仍然不算**：把三类之外的所有 kind 都过一遍 —— 排队
//     （已获批、等锁）、正在部署、阈值内、已完成、未知状态，一个都不许进 overdue。
for (const [label, run, want] of [
  ["已获批、刚开始排队", { status: "waiting", totalCount: 1, pendingCount: 0, createdAt: "2026-09-30T15:25:00Z" }, 0],
  ["已获批、排队超阈值（运行器可能离线）", { status: "waiting", totalCount: 1, pendingCount: 0, createdAt: "2026-09-30T06:33:29Z" }, 1],
  ["正在部署", { status: "in_progress", totalCount: 1, pendingCount: 0, createdAt: "2026-09-30T06:33:29Z" }, 0],
  ["阈值内", { status: "waiting", totalCount: 1, pendingCount: 1, createdAt: "2026-09-30T15:25:00Z" }, 0],
  ["已完成", { status: "completed", conclusion: "success", createdAt: "2026-09-30T06:33:29Z" }, 0],
  ["未知状态", { status: "something-new", createdAt: "2026-09-30T06:33:29Z" }, 0],
  ["等批准超阈值", { status: "waiting", totalCount: 1, pendingCount: 1, createdAt: "2026-09-30T06:33:29Z" }, 1],
  ["jobs=0 超阈值", { status: "waiting", totalCount: 0, pendingCount: 0, createdAt: "2026-09-30T06:33:29Z" }, 1],
]) {
  cases.push([`overdueUnfinished · ${label}`, overdueUnfinished([run], { now }).length, want]);
}

let failed = 0;
for (const [name, got, want] of cases) {
  const ok = got === want;
  if (!ok) failed += 1;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}: ${JSON.stringify(got)}${ok ? "" : ` (want ${JSON.stringify(want)})`}`);
}
if (failed) {
  console.error(`\n${failed} 条未通过`);
  process.exit(1);
}
console.log(`\nwatchdog 自检通过（${cases.length} 条）`);
