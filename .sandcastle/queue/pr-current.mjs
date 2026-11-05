import { execFileSync } from 'node:child_process';
import { appendFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export function validatePr(pr) {
  if (!pr || !Number.isInteger(pr.number) || !['open', 'closed'].includes(pr.state)) {
    throw new Error('PR response is incomplete');
  }
}

export function current(pr, repo, number, head) {
  validatePr(pr);
  return pr.number === Number(number) && pr.state === 'open' && !pr.merged
    && pr.user?.login === repo.split('/')[0] && pr.head?.repo?.full_name === repo
    && pr.base?.repo?.full_name === repo && pr.head?.sha === head;
}

export function readPr(repo, number) {
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo ?? '') || !/^[1-9]\d*$/.test(number ?? '')) {
    throw new Error('Repository and PR number are required');
  }
  const pr = JSON.parse(execFileSync('gh', ['api', `repos/${repo}/pulls/${number}`], {
    encoding: 'utf8', timeout: 20000, stdio: ['ignore', 'pipe', 'pipe'],
  }));
  validatePr(pr);
  return pr;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { GH_REPO, PR_NUMBER, BRANCH_HEAD_SHA, GITHUB_OUTPUT } = process.env;
  if (!/^[0-9a-f]{40}$/.test(BRANCH_HEAD_SHA ?? '')) throw new Error('Expected PR head is required');
  const proceed = current(readPr(GH_REPO, PR_NUMBER), GH_REPO, PR_NUMBER, BRANCH_HEAD_SHA);
  console.log(proceed ? 'PR is current' : 'PR is closed or superseded; no agent delivery');
  if (process.argv.includes('--require')) {
    if (!proceed) process.exitCode = 1;
  } else {
    if (!GITHUB_OUTPUT) throw new Error('GITHUB_OUTPUT is required');
    appendFileSync(GITHUB_OUTPUT, `proceed=${proceed}\n`);
  }
}
