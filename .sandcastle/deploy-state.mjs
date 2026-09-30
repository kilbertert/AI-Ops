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
  // `totalCount === 0` 时**不判定**为死锁：那是「作业还没被调度」的合法中间态，
  // 也可能是我们自己取数失败。判成死锁会把一次正常的慢启动报成故障（假阳性），
  // 而假阳性正是「以后没人看这个告警」的原因。⇒ 直接说不知道。
  if (totalCount === 0) {
    return { kind: "indeterminate", ageMinutes, reason: "作业尚未创建，无法区分死锁与排队" };
  }
  if (pendingCount !== null && pendingCount > 0) {
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
 * 汇总一份判决。**取数为空时不报「正常」** —— 取不到就说取不到（fail honest）。
 *
 * @param {{runs: Array, fetchedOk?: boolean, now?: Date, stuckMinutes?: number,
 *          straightFailures?: number}} input
 */
export function assess(input) {
  const runs = Array.isArray(input.runs) ? input.runs : [];
  if (input.fetchedOk === false) {
    return { ok: false, alarms: [], notes: ["取数失败：本状态**未知**，不是正常"] };
  }
  const run = classifyRun(runs[0] ?? {}, {
    now: input.now,
    stuckMinutes: input.stuckMinutes,
  });
  const straight = straightFailures(runs);
  const straightLimit = input.straightFailures ?? DEFAULT_STRAIGHT_FAILURES;
  const alarms = [];

  if (run.kind === "stuck" || run.kind === "deadlocked") {
    alarms.push({ kind: run.kind, reason: run.reason });
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

  // 历史死锁形态：run 存在、waiting、jobs=0（本票背景里那次）
  const dead = classifyRun(
    { ...base, totalCount: 0, pendingCount: 0 },
    { now },
  );
  console.assert(dead.kind === "indeterminate", "jobs=0 不得判成死锁", dead);

  // 死锁（有作业、无人等批准）
  const locked = classifyRun({ ...base, totalCount: 1, pendingCount: 0 }, { now });
  console.assert(locked.kind === "deadlocked", "有作业且无 pending 应为 deadlocked", locked);

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
