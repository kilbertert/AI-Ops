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

  const overdue = ageMinutes !== null && ageMinutes > stuckMinutes;
  if (!overdue) {
    return { kind: "in-flight", ageMinutes, reason: "未完成，但在阈值内 —— 正常" };
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
      ageMinutes,
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
    return {
      kind: "deadlocked",
      ageMinutes,
      reason: `超过 ${stuckMinutes} 分钟仍未创建任何作业（jobs=0）—— 与"没人点批准"不同，这是卡死了`,
    };
  }
  if (pendingCount > 0) {
    return {
      kind: "stuck",
      ageMinutes,
      reason: `等待批准已超过 ${stuckMinutes} 分钟 —— 部署尚未发生`,
    };
  }
  return {
    kind: "deadlocked",
    ageMinutes,
    reason: `无任何部署在等批准却已超过 ${stuckMinutes} 分钟 —— 与"没人点批准"不同，这是卡住了`,
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
 * 该等多久等多久 —— 那不是 stuck，是排队。所以只有当**最早的**那个未完成 run
 * 本身就超期时才算异常：锁一次只放一个 run 进去，前面那个还没走，后面的必然等。
 *
 * @returns {Array} 超期的未完成 run（含最新那条；正常排队的不在内）
 */
export function overdueUnfinished(runs, { now, stuckMinutes } = {}) {
  const unfinished = runs.filter((run) => UNFINISHED.has(String(run.status ?? "")));
  if (unfinished.length === 0) return [];
  const oldest = unfinished[unfinished.length - 1];
  const verdict = classifyRun(oldest, { now, stuckMinutes });
  if (verdict.kind === "stuck" || verdict.kind === "deadlocked" || verdict.kind === "indeterminate") {
    return [{ run: oldest, verdict }];
  }
  return [];
}

/**
 * 汇总一份判决。**取数不成立时一律不报「正常」** —— 不知道就说不知道（fail honest）。
 *
 * 三种「不成立」都要报警，而不是静默：
 *  - `fetchedOk === false`：连 run 列表都取不到；
 *  - 列表为空：`cd.yml` 从未被触发过，或取数被静默截断 —— 两种都值得看一眼，
 *    因为「一次都没有」与「一直正常」在告警面上是同一种安静；
 *  - 最新 run 的判定是 `indeterminate`（详情取不到、或状态不认识）：**未知不是正常**。
 *
 * @param {{runs: Array, fetchedOk?: boolean, now?: Date, stuckMinutes?: number,
 *          straightFailures?: number}} input
 */
export function assess(input) {
  const runs = Array.isArray(input.runs) ? input.runs : [];
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

  const run = classifyRun(runs[0], {
    now: input.now,
    stuckMinutes: input.stuckMinutes,
  });
  const straight = straightFailures(runs);
  const straightLimit = input.straightFailures ?? DEFAULT_STRAIGHT_FAILURES;

  // 扫**所有**未完成的 run，而不只是最新那条 —— 否则一个卡住的旧 run 会被后来的
  // 新 run 挤出视野，故障"自愈"（上线当天实测撞到过）。
  for (const { verdict } of overdueUnfinished(runs, {
    now: input.now,
    stuckMinutes: input.stuckMinutes,
  })) {
    alarms.push({ kind: verdict.kind, reason: verdict.reason });
  }
  if (run.kind === "indeterminate" && !alarms.some((a) => a.kind === "indeterminate")) {
    alarms.push({ kind: "indeterminate", reason: run.reason });
  }
  if (straight >= straightLimit) {
    alarms.push({
      kind: "straight-failures",
      reason: `最近 ${straight} 次**已完成**的 CD 都不是 success（阈值 ${straightLimit}）`,
    });
  }
  return { ok: alarms.length === 0, alarms, notes: [], latest: run, straight };
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

  // 死锁（有作业、无人等批准）
  const locked = classifyRun({ ...base, totalCount: 1, pendingCount: 0 }, { now });
  console.assert(locked.kind === "deadlocked", "有作业且无 pending 应为 deadlocked", locked);

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
