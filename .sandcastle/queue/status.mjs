import { execFileSync } from 'node:child_process';

export function runnerSummary(runners) {
  if (!Array.isArray(runners)) return { kind: 'unknown', message: '部署runner清单不可读' };
  const matching = runners.filter(r => r.labels?.some(l => l.name?.toLowerCase() === 'aiops-deploy'));
  if (!matching.length) return { kind: 'missing', message: '没有匹配aiops-deploy的runner' };
  if (matching.some(r => r.labels.some(l => l.name.toLowerCase() === 'self-hosted'))) {
    return { kind: 'shared', message: '部署runner误带self-hosted标签，agent可能抢占' };
  }
  if (matching.some(r => !['online', 'offline'].includes(r.status) || typeof r.busy !== 'boolean')) return runnerSummary(null);
  const online = matching.filter(r => r.status === 'online');
  return { kind: online.length ? (online.every(r => r.busy) ? 'busy' : 'available') : 'offline',
    message: `部署runner：匹配${matching.length}，在线${online.length}，忙碌${online.filter(r => r.busy).length}` };
}

export function fetchRunnerSummary() {
  try {
    // Runners API needs repository administration:read, unavailable to GITHUB_TOKEN.
    if (!process.env.RUNNER_READ_TOKEN) return runnerSummary(null);
    const env = { ...process.env, GH_TOKEN: process.env.RUNNER_READ_TOKEN };
    delete env.RUNNER_READ_TOKEN;
    const data = JSON.parse(execFileSync('gh', ['api', 'repos/{owner}/{repo}/actions/runners?per_page=100'], {
      env, encoding: 'utf8', timeout: 20000,
      stdio: ['ignore', 'pipe', 'pipe'],
    }));
    return runnerSummary(data.total_count <= data.runners.length ? data.runners : null);
  } catch {
    return runnerSummary(null);
  }
}
