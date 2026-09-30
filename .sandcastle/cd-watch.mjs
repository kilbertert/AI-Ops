#!/usr/bin/env node
/**
 * CD 状态守望：#407 的信号端。取数 → 判定 → 有异常时**开一张票**。
 *
 * 判定在 `deploy-state.mjs`（纯函数、可自检）；本文件只负责 GitHub API 与告警投递。
 *
 * ## 为什么用「开票」而不是「失败」
 *
 * 判据是**状态**（有没有待批准的部署、最近几次成功没有），不是**动作**。定时任务失败
 * 只会让红色出现在一个没人天天看的页面上 —— 那正是本票要修的病（静默失效）。
 * 开票会出现在 issue 列表里，且**一张未关的票就是未处理的告警**。
 *
 * ## 为什么直接调 `gh` 而不走 workflow cache
 *
 * 本仓的 AFK 工作流把 runner HOME 当缓存、只在需要时 `npm ci`。本脚本只用 `gh`，
 * 不引入依赖解析的失败面 —— 一个「检查部署状态」的脚本自己因为依赖装不上而静默，
 * 就复现了它要修的形态。
 *
 * 用法：`gh auth login` 之后 `node .sandcastle/cd-watch.mjs [--dry-run] [--limit N]`
 */

import { execFileSync } from "node:child_process";
import {
  DEFAULT_STRAIGHT_FAILURES,
  DEFAULT_STUCK_MINUTES,
  assess,
} from "./deploy-state.mjs";

export const ALARM_LABEL = "cd:needs-attention";
const ALARM_TITLE = "CD 状态：有部署尚未发生或连续未成功";

function gh(args) {
  return execFileSync("gh", args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
}

function enrich(runs) {
  // 两个只有 job/部署层面才知道的量：等批准的**部署记录**与作业数。
  // 它们正是「死锁 vs 等批准」的唯一区分点，值得每次多两次调用。
  return runs.map((run) => {
    let totalCount = null;
    let pendingCount = null;
    try {
      totalCount = JSON.parse(
        gh(["api", `repos/{owner}/{repo}/actions/runs/${run.databaseId}/jobs`, "--jq", ".total_count"]),
      );
      pendingCount = JSON.parse(
        gh([
          "api",
          `repos/{owner}/{repo}/actions/runs/${run.databaseId}/pending_deployments`,
          "--jq",
          "length",
        ]),
      );
    } catch {
      // 取不到就是 null（未知），判定侧按「不知道」处理，不按「没有」处理。
    }
    return { ...run, totalCount, pendingCount };
  });
}

/** 取最近若干次 CD run 的形态；取不到返回 `fetchedOk: false`（不抛）。 */
export function fetchRuns({ repo, workflow = "cd.yml", limit = 10 } = {}) {
  const args = [
    "run",
    "list",
    "--workflow",
    workflow,
    "--limit",
    String(limit),
    "--json",
    "databaseId,status,conclusion,createdAt,headSha",
  ];
  if (repo) args.push("--repo", repo);
  let runs;
  try {
    runs = JSON.parse(gh(args));
  } catch (error) {
    return { runs: [], fetchedOk: false, error: String(error.message ?? error) };
  }
  return { runs: enrich(runs), fetchedOk: true };
}

/**
 * 取**全部未完成**的 run —— 不受「最近 N 次」窗口限制。
 *
 * 🔴 为什么必须单独取：上限为 N 的窗口会让一个旧的卡住 run **在 N 次更新的 run
 * 之后从视野里消失**（评审指出）。而"卡住"的定义恰恰是"它一直没结束" ——
 * 用「最近 N 次」去找它，等于用一个会随时间收窄的窗口去找一个**随时间变得更该被
 * 看见**的东西。
 *
 * ⚠️ 判据靠这条的**完整性**：`jobs=0` ⇒ 死锁这条只有在拿到该 run 的**完整**列表时
 * 才成立。所以这里按**每个状态**分别取（`--status` 是精确过滤，不做窗口截断），
 * 任一状态取不到就整体报「取数失败」—— **宁可报未知，也不返回部分结果**。
 */
export function fetchUnfinished({ repo, workflow = "cd.yml", limit = 100 } = {}) {
  const statuses = ["queued", "in_progress", "waiting", "requested", "pending"];
  const collected = [];
  for (const status of statuses) {
    const args = [
      "run",
      "list",
      "--workflow",
      workflow,
      "--status",
      status,
      "--limit",
      String(limit),
      "--json",
      "databaseId,status,conclusion,createdAt,headSha",
    ];
    if (repo) args.push("--repo", repo);
    let runs;
    try {
      runs = JSON.parse(gh(args));
    } catch (error) {
      return { runs: [], fetchedOk: false, error: `status=${status}: ${error.message ?? error}` };
    }
    // 取满上限说明可能还有更多 ⇒ **不完整**，不能拿它判 jobs=0。
    if (runs.length >= limit) {
      return { runs: [], fetchedOk: false, error: `status=${status} 命中上限 ${limit}，结果可能不完整` };
    }
    collected.push(...runs);
  }
  return { runs: enrich(collected), fetchedOk: true };
}

function announce(title, body, label) {
  try {
    gh(["label", "create", label, "--color", "d93f0b", "--description", "CD 未按预期发生"]);
  } catch {
    // 标签已存在即成功；这里不因它失败而中断。
  }
  const open = JSON.parse(
    gh(["issue", "list", "--state", "open", "--label", label, "--json", "number", "--limit", "10"]),
  );
  if (open.length > 0) {
    // 已有未关的票 ⇒ 追加一条评论，不重复开票。一张未关的票本身就是「还没处理」。
    gh(["issue", "comment", String(open[0].number), "--body", body]);
    return { kind: "commented", number: open[0].number };
  }
  const url = gh(["issue", "create", "--title", title, "--body", body, "--label", label]).trim();
  return { kind: "created", url };
}

function main() {
  const dryRun = process.argv.includes("--dry-run");
  const limitArg = process.argv.indexOf("--limit");
  const limit = limitArg > -1 ? Number(process.argv[limitArg + 1]) : 10;
  // 两条取数各司其职，都失败才算取数失败：
  //   · `fetchUnfinished` —— **全部**未完成的 run（判「有没有谁卡住」；不受窗口截断）；
  //   · `fetchRuns` —— 最近 N 次（判「连续多少次没成功」；这条本来就只看最近）。
  const unfinished = fetchUnfinished({ limit: 100 });
  const recent = fetchRuns({ limit });
  // 🔴 **两份取数必须都成功才算取数成功** —— 它们服务两条**独立**判据，任何一份失败
  // 都意味着有一条判据无法成立。用 `||` 合并会让「最近取数失败 + 未完成取数成功」
  // 变成「一切正常」：那时连续失败计数喂的是未完成列表（全是未完成 ⇒ 计数恒为 0），
  // 恰好把「连续 N 次没成功」这条判据变成永远不响（评审指出）。
  const fetchedOk = unfinished.fetchedOk && recent.fetchedOk;
  const error = unfinished.error ?? recent.error;
  // 「有没有谁卡住」吃**全部未完成**的 run；「连续多少次没成功」吃**最近 N 次**。
  // 两份数据不能互换 —— 用窗口去找卡住的 run 等于用一个随时间收窄的窗口去找一个
  // 随时间更该被看见的东西。
  const result = assess({
    runs: recent.runs,
    // `null` = 这份取数失败（**不是**「没有未完成的 run」）—— 空数组与 null 含义不同。
    unfinishedRuns: unfinished.fetchedOk ? unfinished.runs : null,
    fetchedOk,
    stuckMinutes: DEFAULT_STUCK_MINUTES,
    straightFailures: DEFAULT_STRAIGHT_FAILURES,
  });

  if (result.ok) {
    console.log("CD 状态正常");
    return;
  }
  // 取数失败也算异常，但**不冒充**成上面两种 —— 报「未知」比报「正常」诚实。
  const lines = ["## CD 状态异常", ""];
  for (const alarm of result.alarms) lines.push(`- **${alarm.kind}**：${alarm.reason}`);
  if (!fetchedOk) lines.push(`- 取数失败：${error ?? "unknown"}`);
  if (result.latest) {
    lines.push("");
    lines.push(
      `最近一次：\`${result.latest.kind}\`（${result.latest.reason}）` +
        `，最近**已完成**的 CD 里开头连续 ${result.straight} 次不是 success。`,
    );
  }
  lines.push(
    "",
    "**批准门不因此改动**：`production-41` 的 `required_reviewers` 保留。",
    "卡住时怎么办、谁负责批准，见 `docs/agents/env-41-runbook.md` 的 CD 一节。",
  );
  const body = lines.join("\n");
  if (dryRun) {
    console.log(body);
    return;
  }
  const outcome = announce(ALARM_TITLE, body, ALARM_LABEL);
  console.log(JSON.stringify(outcome));
}

if (process.argv[1] && process.argv[1].endsWith("cd-watch.mjs")) main();
