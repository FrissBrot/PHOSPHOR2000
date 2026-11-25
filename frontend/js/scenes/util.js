// ISO local time "YYYY-MM-DDTHH:MM:SS" (no zone => local wall clock of the cluster)
export function parseTarget(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?$/.exec(s || "");
  if (!m) return new Date(2027, 1, 5, 20, 0, 0);
  return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
}
