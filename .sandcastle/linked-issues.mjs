// Refs retains task ownership without claiming that merge completes acceptance.
export function linkedIssues(body = "") {
  return [...new Set([...body.matchAll(/\b(?:closes|fixes|resolves|refs)\s+#(\d+)\b/gi)]
    .map((match) => Number(match[1])))];
}
