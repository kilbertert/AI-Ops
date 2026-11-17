// Refs retains task ownership without claiming that merge completes acceptance.
export function linkedIssues(body = "") {
  return [...new Set([...body.matchAll(/\b(?:closes|fixes|resolves|refs)\s+#(\d+)\b/gi)]
    .map((match) => Number(match[1])))];
}

export function primaryIssue(body = "") {
  const closing = body.match(/\b(?:closes|fixes|resolves)\s+#(\d+)\b/i);
  return closing ? Number(closing[1]) : linkedIssues(body)[0];
}
