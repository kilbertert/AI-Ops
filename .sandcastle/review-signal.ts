/** Only an unquoted final marker outside fenced code is a verdict. */
export function strictSignal(stdout: string): string | undefined {
  let fence: string | undefined;
  let verdict: string | undefined;
  const seen = new Set<string>();
  for (const line of stdout.replace(/\r\n?/g, "\n").split("\n")) {
    if (!line.trim()) continue;
    verdict = undefined;
    const marker = /^[ ]{0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (fence) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && /^[ \t]*$/.test(marker[2])) {
        fence = undefined;
      }
      continue;
    }
    if (marker) {
      fence = marker[1];
      continue;
    }
    verdict = /^<promise>(COMPLETE|BLOCKED)<\/promise>[ \t]*$/.exec(line)?.[1];
    if (verdict) seen.add(verdict);
  }
  return !fence && seen.size === 1 ? verdict : undefined;
}
