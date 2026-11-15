// Local interview history (browser localStorage only - nothing leaves the machine).
// Each finished interview is stored with its setup, transcript and feedback report, so that:
//   - the Feedback tab can show past reports and progress per criterion;
//   - the next interviewer can probe the areas the candidate scored low on (with new questions);
//   - the AI demo can be steered to the candidate's weak questions and show strong answers to them.

const KEY = "ai-avatar.history.v1";
const MAX = 40;

export function loadHistory() {
  try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { return []; }
}
function store(list) {
  try { localStorage.setItem(KEY, JSON.stringify(list.slice(-MAX))); } catch (e) { console.warn("history not saved", e); }
}
export function saveSession(session) {
  const list = loadHistory();
  session.id = session.id || Date.now().toString(36);
  list.push(session);
  store(list);
  return session.id;
}
export function deleteSession(id) { store(loadHistory().filter((s) => s.id !== id)); }
export function clearHistory() { try { localStorage.removeItem(KEY); } catch {} }

export const avgScore = (report) => {
  const xs = (report?.scores || [])
    .filter((s) => s.verified && Number.isFinite(+s.score) && +s.score >= 1 && +s.score <= 4)
    .map((s) => +s.score);
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
};

// Past real interviews (not demos) with a report, newest first.
export const scoredInterviews = (history = loadHistory()) =>
  history.filter((s) => s.mode !== "demo" && (s.report?.scores || []).some((score) =>
    score.verified && Number.isFinite(+score.score) && +score.score >= 1 && +score.score <= 4))
    .sort((a, b) => b.when - a.when);

// Per-criterion change versus the previous scored interview (matched by criterion name).
export function progress(report, history = loadHistory(), setup = null) {
  const prev = scoredInterviews(history).find((s) => {
    if (!setup) return true;
    const prior = s.setup || {};
    return norm(prior.role) === norm(setup.role) &&
      norm(prior.seniority) === norm(setup.seniority) &&
      norm(prior.type) === norm(setup.type) &&
      typeof prior.rubric === "string" && prior.rubric === setup.rubric;
  });
  if (!prev) return null;
  const by = Object.fromEntries((prev.report.scores || [])
    .filter((s) => s.verified && Number.isFinite(+s.score) && +s.score >= 1 && +s.score <= 4)
    .map((s) => [norm(s.criterion), +s.score]));
  const deltas = {};
  for (const s of report.scores || []) {
    if (s.verified && Number.isFinite(+s.score) && +s.score >= 1 && +s.score <= 4 && norm(s.criterion) in by)
      deltas[s.criterion] = +s.score - by[norm(s.criterion)];
  }
  if (!Object.keys(deltas).length) return null;
  return { when: prev.when, deltas, prevAvg: avgScore(prev.report), prevRecommendation: prev.report.recommendation };
}
const norm = (x) => String(x || "").toLowerCase().replace(/[^a-z]+/g, " ").trim();

// What the previous interviews say about this candidate, for the prompts (bounded size).
export function learnerBrief(history = loadHistory(), { max = 3 } = {}) {
  const past = scoredInterviews(history).slice(0, max);
  if (!past.length) return null;
  const weak = [], strong = [], practice = [], improvements = [], asked = [];
  for (const s of past) {
    for (const c of s.report.scores || []) {
      if (!Number.isFinite(+c.score) || +c.score < 1 || +c.score > 4) continue;
      if (!c.verified) continue; // unverified model scores must not become personalized coaching facts
      // Only carry forward evidence that passed transcript verification. A model-generated
      // quote that was not found in the candidate's transcript must not become coaching context.
      const q = (c.checked || []).find((x) => x.ok)?.q;
      if (+c.score <= 2) {
        weak.push({ criterion: c.criterion, score: +c.score, quote: q, improve: c.improve });
        if (c.improve) improvements.push(c.improve);
      }
      else if (+c.score >= 4) strong.push(c.criterion);
    }
    practice.push(...(s.report.practice_questions || []));
    asked.push(...(s.transcript || []).filter((t) => t.who === "Interviewer" && /\?/.test(t.text)).map((t) => t.text.split(/(?<=\?)/)[0].slice(0, 140)));
  }
  const uniq = (xs) => [...new Set(xs.filter(Boolean))];
  return {
    count: past.length, lastWhen: past[0].when, lastRole: `${past[0].setup?.seniority || ""} ${past[0].setup?.role || ""}`.trim(),
    lastRecommendation: past[0].report.recommendation, lastAvg: avgScore(past[0].report),
    weak: weak.slice(0, 6), strong: uniq(strong).slice(0, 5), practice: uniq(practice).slice(0, 6),
    improvements: uniq(improvements).slice(0, 6), asked: uniq(asked).slice(0, 12),
  };
}

// Text block for the interviewer / feedback prompts.
export function briefText(b) {
  if (!b) return "";
  const date = new Date(b.lastWhen).toLocaleDateString();
  return [
    `This candidate has practised ${b.count} interview(s) here before. Last one (${date}, ${b.lastRole || "role unspecified"}): ` +
      `${b.lastRecommendation || "no recommendation"}${b.lastAvg ? `, average score ${b.lastAvg.toFixed(1)}/4` : ""}.`,
    b.weak.length && "They scored LOW on:\n" + b.weak.map((w) => `- ${w.criterion} (${w.score}/4)${w.quote ? `: they said "${w.quote}"` : ""}${w.improve ? ` -> advice given: ${w.improve}` : ""}`).join("\n"),
    b.strong.length && `They scored well on: ${b.strong.join(", ")}.`,
    b.improvements.length && "Improvements they were asked to work on:\n" + b.improvements.map((x) => `- ${x}`).join("\n"),
    b.asked.length && "Questions already asked in previous interviews (ask DIFFERENT ones):\n" + b.asked.map((x) => `- ${x}`).join("\n"),
  ].filter(Boolean).join("\n");
}
