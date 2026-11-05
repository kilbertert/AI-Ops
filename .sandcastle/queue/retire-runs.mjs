import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { readPr } from './pr-current.mjs';

const workflows = new Map([
  ['.github/workflows/agent-implement-pr.yml', 'Agent Implement (PR)'],
  ['.github/workflows/agent-review.yml', 'Agent Review'],
  ['.github/workflows/agent-update-branch.yml', 'Agent Update Branch'],
]);
const active = new Set(['queued', 'pending', 'waiting', 'requested', 'in_progress']);

export function prNumber(run) {
  const name = workflows.get(run.path);
  if (!name || run.event !== 'pull_request_target' || !active.has(run.status)) return null;
  const prefix = `${name} / pr-`;
  const number = run.display_title?.startsWith(prefix) ? run.display_title.slice(prefix.length) : '';
  return /^[1-9]\d*$/.test(number) ? number : null;
}

export function obsolete(run, pr, repo, closedAt) {
  const created = Date.parse(run.created_at);
  const closed = Date.parse(pr.closed_at);
  return prNumber(run) === String(pr.number) && pr.state === 'closed'
    && pr.user?.login === repo.split('/')[0] && pr.head?.repo?.full_name === repo
    && pr.base?.repo?.full_name === repo && run.repository?.full_name === repo
    && run.head_branch === pr.head.ref && Number.isFinite(created) && Number.isFinite(closed)
    && created <= closed && (!closedAt || pr.closed_at === closedAt);
}

function api(repo, suffix, ...options) {
  return execFileSync('gh', ['api', `repos/${repo}/${suffix}`, ...options], {
    encoding: 'utf8', timeout: 20000, stdio: ['ignore', 'pipe', 'pipe'],
  });
}

export function completePages(pages) {
  if (!Array.isArray(pages) || !pages.length || pages.some(p => !Array.isArray(p.workflow_runs))) {
    throw new Error('Incomplete run pages');
  }
  const runs = pages.flatMap(p => p.workflow_runs);
  const ids = new Set(runs.map(run => run.id));
  if (ids.size !== runs.length || runs.some(run => !Number.isSafeInteger(run.id) || run.id <= 0)) {
    throw new Error("Duplicate or invalid run IDs; listing is not complete");
  }
  if (pages.some(p => !Number.isInteger(p.total_count) || p.total_count > runs.length)) {
    throw new Error('Run listing was truncated; refusing partial cleanup');
  }
  return runs;
}

export async function retire(repo, { number, closedAt, dryRun = false } = {}) {
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo ?? '')) throw new Error('GH_REPO is required');
  if (number && !/^[1-9]\d*$/.test(number)) throw new Error('Invalid PR number');
  // Read every page before making the first cancellation request.
  const runs = [];
  for (const status of active) {
    const pages = JSON.parse(api(repo, `actions/runs?status=${status}&per_page=100`, '--paginate', '--slurp'));
    runs.push(...completePages(pages));
  }
  const cancelled = new Set();
  for (const candidate of runs) {
    const pr = prNumber(candidate);
    if (!pr || (number && pr !== number) || cancelled.has(candidate.id)) continue;
    const run = JSON.parse(api(repo, `actions/runs/${candidate.id}`));
    // Re-read immediately before mutation; a delayed closed event must not act on a reopened PR.
    const live = readPr(repo, pr);
    if (!obsolete(run, live, repo, closedAt)) continue;
    console.log(`${dryRun ? 'Would cancel' : 'Cancel'} ${run.id}: PR #${pr} closed`);
    if (!dryRun) api(repo, `actions/runs/${run.id}/cancel`, '--method', 'POST');
    cancelled.add(run.id);
  }
  if (dryRun) return;
  const deadline = Date.now() + 120000;
  while (cancelled.size && Date.now() < deadline) {
    for (const id of cancelled) {
      if (JSON.parse(api(repo, `actions/runs/${id}`)).status === 'completed') {
        console.log(`Run ${id}: terminal state confirmed`);
        cancelled.delete(id);
      }
    }
    if (cancelled.size) await new Promise(resolve => setTimeout(resolve, 5000));
  }
  if (cancelled.size) throw new Error(`Cancellation not confirmed: ${[...cancelled].join(', ')}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await retire(process.env.GH_REPO, {
    number: process.env.CLOSED_PR || undefined, closedAt: process.env.CLOSED_AT || undefined,
    dryRun: process.argv.includes('--dry-run'),
  });
}
