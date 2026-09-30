/**
 * CD 的真实状态：#407。把「部署到底发生没有」变成一个可回答的问题。
 *
 * 起因是一次**连续 6 次 src 合并都没成功部署、而无人察觉**（记录见
 * `docs/validation.md`）：那段时间有人在手工 rsync，生产确实更新了，于是没有任何
 * 理由去看 CD —— 手工动作把「CD 坏了」这条本应被发现的信号一起抹掉了。
 *
 * 两条判据各自独立，对应两种不同的失败形态：
 *
 *  1. **run 卡住**：`waiting` 与「根本没在跑」在 API 上长得一样（run 存在、
 *     状态 waiting/pending、jobs=0）。只盯「超时未批准」会把它误判为「没人点批准」。
 *     ⇒ 用 `pending_deployments` 区分：等批准的先有部署记录，死锁的没有。
 *  2. **连续不成功**：单次失败会被归因（「这次有问题」），而**连续不成功看起来和
 *     「还没来得及」一样** —— 那 6 次就是这样过去的。⇒ 判据是「最近 N 次**已完成**的
 *     run 里有没有 success」，不是「当前这次多久没动静」。
 *
 * 本模块只做判定（纯函数），取数与告警在 `cd-watch.mjs`。
 */

/** 未完成的 run 在 API 上的状态集合（GitHub 用 request 表示等待批准）。 */
const UNFINISHED = new Set(["queued", "in_progress", "waiting", "requested", "pending"]);

export const DEFAULT_STUCK_MINUTES = 30;
export const DEFAULT_STRAIGHT_FAILURES = 3;

/**
 * 这些 kind 是**要报警**的（它们是「部署没有发生」的不同成因）；其余是正常/中间态。
 *
 * 抽成常量是因为上一轮把它写成三处零散的 `||`：一处漏改就会出现「某类异常不再进告警
 * 而用例仍通过」——评审正是在那里抓到一个**永远为真**的断言。
 */
const ALARMING = new Set(["stuck", "deadlocked", "indeterminate", "queued-too-long"]);

/**
 * 一个 run 的形态。字段全部来自 GitHub API，本函数不做取数。
 *
 * @param {{status?: string, conclusion?: string|null, createdAt?: string,
 *          totalCount?: number|null, pendingCount?: number|null}} run
 * @param {{now?: Date, stuckMinutes?: number}} [options]
 * @returns {{kind: string, ageMinutes: number|null, reason: string}}
 */
export function classifyRun(run, options = {}) {
  const now = options.now ?? new Date();
  const stuckMinutes = options.stuckMinutes ?? DEFAULT_STUCK_MINUTES;
  const status = String(run.status ?? "");
  const conclusion = run.conclusion ?? null;
  const created = run.createdAt ? Date.parse(run.createdAt) : Number.NaN;
  const ageMinutes = Number.isFinite(created) ? (now.getTime() - created) / 60000 : null;

  if (status === "completed") {
    return {
      kind: conclusion === "success" ? "deployed" : "failed",
      ageMinutes,
      reason: conclusion === "success" ? "已完成并成功" : `已完成但未成功：${conclusion ?? "unknown"}`,
    };
  }

  if (!UNFINISHED.has(status)) {
    return { kind: "unknown", ageMinutes, reason: `未知状态：${status || "(空)"}` };
  }

  // 🔴 **`in_progress` 永远不是 stalled。** 它是在**真的干活**（部署脚本正在跑），
  //    而不是在等谁做什么。这条判据挂在**创建时刻**上，会随时间必然触发：
  //    一次"等批准 40 分钟、然后开始部署 10 分钟"的运行，会在第二个阶段被判成
  //    `deadlocked`（因为 `pending_deployments` 在批准后归零）—— 纯假阳性。
  //    本 job 自己就有 20 分钟超时兜着，不需要外部再加一层。
  //    （评审指出；这条也是"不能只看创建时刻"的第二个例子。）
  const ageMinutesWait = ageMinutes;
  if (status === "in_progress") {
    return { kind: "in-flight", ageMinutes: ageMinutesWait, reason: "正在部署 —— 不看时长" };
  }

  const overdue = ageMinutesWait !== null && ageMinutesWait > stuckMinutes;
  if (!overdue) {
    return { kind: "in-flight", ageMinutes: ageMinutesWait, reason: "未完成，但在阈值内 —— 正常" };
  }

  const totalCount = run.totalCount ?? null;
  const pendingCount = run.pendingCount ?? null;

  // 🔴 死锁与等待批准的**唯一**可区分点。API 不会替我们分辨：
  //    两者都是 waiting、jobs 数也一样。有没有 pending deployment 才是判据。
  //
  // 这一段的每一条都要分清「是 0」与「取不到」。把 null（查询失败）当成 0 会把
  // 「不知道」报成「死锁」；反过来把 0 当成 null 会让真正的死锁一直静默。
  // 曾经写错过一次：`pendingCount !== null && pendingCount > 0` 后面直接接
  // deadlocked，于是**取数失败被报成死锁**（评审指出）。
  if (totalCount === null || pendingCount === null) {
    const missing = totalCount === null ? "作业数" : "待批准部署数";
    return {
      kind: "indeterminate",
      ageMinutes: ageMinutesWait,
      reason: `超过 ${stuckMinutes} 分钟未完成，但${missing}取不到 —— **状态未知**，不是正常`,
    };
  }
  // jobs=0 且已经超时 ⇒ **这正是那次死锁的形态**：run 一创建就占住并发槽位，
  // 从未调度出作业，于是既没有要批准的部署、也没有任何推进。
  //
  // 最初这里判 indeterminate（担心「作业还没被调度」是合法中间态），**那是错的**：
  // 阈值本身就是"已经等了 30 分钟"，一个 30 分钟还没建出作业的 run 不是慢，是坏了。
  // 判 indeterminate ⇒ assess 报正常 ⇒ 恰好把本票要修的那种静默又做了一遍。
  if (totalCount === 0) {
    // ⚠️ 这条判据有前提：**必须取到全部未完成的 run**（见 fetchUnfinished）。
    // 只看「最近 N 次」时，`totalCount=0` 也会出现在「取数只拿到了一部分」的截断情形，
    // 那时它说明的是取数不完整，不是死锁。调用方负责这个前提。
    return {
      kind: "deadlocked",
      ageMinutes: ageMinutesWait,
      reason: `超过 ${stuckMinutes} 分钟仍未创建任何作业（jobs=0）—— 与"没人点批准"不同，这是卡死了`,
    };
  }
  if (pendingCount > 0) {
    return {
      kind: "stuck",
      ageMinutes: ageMinutesWait,
      reason: `等待批准已超过 ${stuckMinutes} 分钟 —— 部署尚未发生`,
    };
  }
  // 剩下的都是「作业已创建、没有待批准部署」：要么在跑（`in_progress` 已在上面返回），
  // 要么**已获批、还没开始** —— 在等并发锁，或在等一个能接活的运行器。
  //
  // ⚠️ **这一段被两个方向各纠过一次，结论是「要判，但别判成死锁」**：
  //   · 先判成 `deadlocked` ⇒ 纯假阳性：合法排队被说成"卡死了"（评审指出）；
  //   · 改成「不看时长」⇒ 另一个洞：**运行器离线时它会永远显示健康**，
  //     而部署根本开不了工（评审再指出）。
  // ⇒ 保留时长判据，但给一个**说得出成因**的类别：既不是"没人点批准"（`stuck`），
  //    也不是"作业没建出来"（`deadlocked`），而是**已获批却开不了工**。
  //
  // 这个阈值有据可依，不是拍的：持锁的那个 job 自己有 20 分钟 `timeout-minutes`，
  // 一次合法排队的上限因此就是"前一个跑完"≈ 20 分钟出头。超过 `stuckMinutes`（30）
  // 仍未开始，成因只剩两种：运行器不在线，或前一个部署自己挂了没释放锁。
  return {
    kind: "queued-too-long",
    ageMinutes: ageMinutesWait,
    reason:
      `已获批却超过 ${stuckMinutes} 分钟仍未开始 —— 合法排队的上限是"前一个部署跑完"` +
      `（它自己的 job 超时是 20 分钟）；超过它，成因是**运行器不在线**或前一个没释放锁`,
  };
}

/**
 * 最近 N 次**已完成**的 run 里，开头连续多少次不是 success。
 *
 * 只看已完成的：正在等批准的那次还没结果，把它算进去会让「刚要部署」也报警。
 */
export function straightFailures(runs) {
  let count = 0;
  for (const run of runs) {
    if (String(run.status) !== "completed") continue;
    if (run.conclusion === "success") break;
    count += 1;
  }
  return count;
}

/**
 * **所有**未完成的 run 里卡住/卡死的那些 —— 不只是最新那一条。
 *
 * 只看 `runs[0]` 会漏掉整类故障且会**自愈**：一个新 push 让更新的 run 成为首条，
 * 旧的卡住 run 就从视野里消失。（这个洞是上线当天实测撞到的：一个等了 84 分钟的
 * run 躺在那儿，而 `assess` 说「正常」。）
 *
 * ⚠️ **但「旧」不等于「坏」**：并发锁只保证**串行**，一个等着接替前一个的 run 本来就
 * 该等多久等多久 —— 那不是 stuck，是排队。所以**逐条**判定，而不只看最早那个：
 * 取数（`fetchUnfinished`）已经保证拿到的是**全部**未完成 run，于是每条都能自己
 * 回答「我超期了吗」。若只看最早那条，一个 `indeterminate` 的旧 run 会把后面
 * 一条**确凿**的 `stuck` 一起挡掉（评审指出）。
 *
 * ⇒ 前提：**调用方必须取全**。截断的列表会让 `jobs=0` 这类判据失真，所以
 * `fetchUnfinished` 宁可报「取数失败」也不返回部分结果。
 *
 * @returns {Array<{run: object, verdict: object}>} 超期的未完成 run（正常排队的不在内）
 */
export function overdueUnfinished(runs, { now, stuckMinutes } = {}) {
  const overdue = [];
  for (const run of runs) {
    if (!UNFINISHED.has(String(run.status ?? ""))) continue;
    // ⚠️ **只有这些才算「卡住」** —— 本函数**已经**筛掉 `in-flight`，因此调用方
    // **不能**用 `verdict.kind !== "in-flight"` 当判据：那会把普通排队也当成异常。
    // 曾经这样写错过一次，而且用例没抓到（断言的是 `assess().ok`，排队两条 run 的
    // 组合恰好仍为 true）。现在由显式枚举把「算」与「不算」**都**钉住 ——
    // 见 `cd-watch.test.mjs` 的 8b。
    const verdict = classifyRun(run, { now, stuckMinutes });
    if (ALARMING.has(verdict.kind)) overdue.push({ run, verdict });
  }
  return overdue;
}

/**
 * 汇总一份判决。**取数不成立时一律不报「正常」** —— 不知道就说不知道（fail honest）。
 *
 * 三种「不成立」都要报警，而不是静默：
 *  - `fetchedOk === false`：任一份取数失败；
 *  - 最近列表为空：`cd.yml` 从未被触发过，或取数被静默截断 —— 两种都值得看一眼，
 *    因为「一次都没有」与「一直正常」在告警面上是同一种安静；
 *  - 任一条待判定的 run 是 `indeterminate`（详情取不到、或状态不认识）：**未知不是正常**。
 *
 * ⚠️ **两条判据吃两份数据，不要混用**：
 *  - 「有没有谁卡住」吃 `unfinishedRuns` —— 它必须是**全部未完成**的 run
 *    （`fetchUnfinished`）。用「最近 N 次」的窗口去找卡住的 run，等于用一个随时间
 *    收窄的窗口去找一个随时间更该被看见的东西（评审指出）。
 *    **空集合是正常的**（没人卡住），不是「取不到」。
 *  - 「连续多少次没成功」吃 `runs` —— 这条本来就只看最近 N 次**已完成**的。
 *    两者合成一份会让未完成的 run 插进来打乱「连续」的计数。
 *
 * @param {{runs: Array, unfinishedRuns?: Array|null, fetchedOk?: boolean, now?: Date,
 *          stuckMinutes?: number, straightFailures?: number}} input
 *    `unfinishedRuns` 为 `null` 表示**这份取数失败**（未完成集合未知）；
 *    空数组表示「确实没有未完成的 run」——两者含义不同，不能混。
 */
export function assess(input) {
  const runs = Array.isArray(input.runs) ? input.runs : [];
  const unfinishedRuns = Array.isArray(input.unfinishedRuns) ? input.unfinishedRuns : null;
  const alarms = [];

  if (input.fetchedOk === false) {
    alarms.push({ kind: "unavailable", reason: "取数失败：本状态**未知**，不是正常" });
    return { ok: false, alarms, notes: [], latest: null, straight: 0 };
  }
  // 空列表有多种成因（从未触发 / 取数被截断 / workflow 被改名），
  // 判据只有一句：**「一次都没有」不构成「一切正常」**。
  if (runs.length === 0) {
    alarms.push({ kind: "no-runs", reason: "取不到任何 CD run —— 要么从未触发过，要么取数不完整" });
    return { ok: false, alarms, notes: [], latest: null, straight: 0 };
  }
  // 未完成集合取不到 ⇒ 未知。它**不能**退化用最近列表代替：那个窗口不完整，
  // 而 `jobs=0` ⇒ 死锁这条判据恰恰依赖完整性。
  if (unfinishedRuns === null) {
    alarms.push({
      kind: "unavailable",
      reason: "未完成 run 的集合取不到 —— 无法判断是否有人卡住（**未知**，不是正常）",
    });
  }

  // 每条未完成的 run **自己**回答「我超期了吗」，而不是只问最早那条 ——
  // 否则一条 `indeterminate` 的旧 run 会把后面一条**确凿**的 `stuck` 一起挡掉
  // （评审指出；这与上一条修的是同一个「只看一条」的毛病）。
  for (const { verdict } of overdueUnfinished(unfinishedRuns ?? [], {
    now: input.now,
    stuckMinutes: input.stuckMinutes,
  })) {
    alarms.push({ kind: verdict.kind, reason: verdict.reason });
  }

  const straight = straightFailures(runs);
  const straightLimit = input.straightFailures ?? DEFAULT_STRAIGHT_FAILURES;
  if (straight >= straightLimit) {
    alarms.push({
      kind: "straight-failures",
      reason: `最近 ${straight} 次**已完成**的 CD 都不是 success（阈值 ${straightLimit}）`,
    });
  }
  // 摘要行用**最新**那条（按创建时间），而不是数组首条 —— 数组顺序不保证是时间序。
  const newest = [...runs].sort((a, b) => Date.parse(b.createdAt ?? 0) - Date.parse(a.createdAt ?? 0))[0];
  return {
    ok: alarms.length === 0,
    alarms,
    notes: [],
    latest: classifyRun(newest, { now: input.now, stuckMinutes: input.stuckMinutes }),
    straight,
  };
}

/** 自检：跑 `node .sandcastle/deploy-state.mjs`。判据用第 407 号票记录的真实形态。 */
function demo() {
  const now = new Date("2026-09-30T15:30:00Z");
  const base = { createdAt: "2026-09-30T06:33:29Z", status: "waiting" };

  // 真实：run 36678842333（58dc271），pending deployment 在等批准
  const waiting = classifyRun({ ...base, totalCount: 1, pendingCount: 1 }, { now });
  console.assert(waiting.kind === "stuck", "等批准超阈值应为 stuck", waiting);

  // 历史死锁形态：run 存在、waiting、jobs=0（本票背景里那次）。
  // 超时仍 jobs=0 ⇒ 判死锁；**曾经判成 indeterminate，那会让 assess 报正常**。
  const dead = classifyRun({ ...base, totalCount: 0, pendingCount: 0 }, { now });
  console.assert(dead.kind === "deadlocked", "超时仍 jobs=0 应为 deadlocked", dead);

  // 已获批、在等并发锁 ⇒ 超阈值时是 `queued-too-long`（不是死锁，也不是"健康"）
  const queued = classifyRun({ ...base, totalCount: 1, pendingCount: 0 }, { now });
  console.assert(queued.kind === "queued-too-long", "已获批却超阈值应为 queued-too-long", queued);

  // 取数失败（null）不得被当成 0
  const unknown = classifyRun({ ...base, totalCount: null, pendingCount: null }, { now });
  console.assert(unknown.kind === "indeterminate", "详情取不到应为 indeterminate", unknown);
  console.assert(
    assess({ runs: [{ ...base, totalCount: null, pendingCount: null }], fetchedOk: true }).ok === false,
    "indeterminate 不得报正常",
  );
  console.assert(assess({ runs: [], fetchedOk: true }).ok === false, "空列表不得报正常");

  // 正常：刚创建 5 分钟
  const fresh = classifyRun(
    { ...base, createdAt: "2026-09-30T15:25:00Z", totalCount: 1, pendingCount: 1 },
    { now },
  );
  console.assert(fresh.kind === "in-flight", "阈值内不得报警", fresh);

  // 连续 6 次 cancelled（票里那段的真实形态）
  const streak = straightFailures([
    { status: "waiting", conclusion: null },
    { status: "completed", conclusion: "cancelled" },
    { status: "completed", conclusion: "cancelled" },
    { status: "completed", conclusion: "cancelled" },
    { status: "completed", conclusion: "success" },
  ]);
  console.assert(streak === 3, "应数到第一个 success 为止", streak);

  // 取数失败不得报正常
  console.assert(assess({ runs: [], fetchedOk: false }).ok === false, "取数失败不能算正常");

  console.log("deploy-state demo passed");
}

if (process.argv[1] && process.argv[1].endsWith("deploy-state.mjs")) demo();
