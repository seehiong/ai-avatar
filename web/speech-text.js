// Make text speakable before TTS (mirror of backend/speechtext.py): espeak reads "50,000" as
// "fifty, zero zero zero", "$1.5M" as "dollar one . five M", "40k" as "forty kay", "900ms" as "nine
// hundred em ess". Only the audio uses this; the transcript keeps the original text.
const CURRENCY = { "S$": "Singapore dollars", "US$": "US dollars", "A$": "Australian dollars", "$": "dollars",
  "€": "euros", "£": "pounds", "¥": "yen", "₹": "rupees" };
const SCALE = { k: "thousand", m: "million", b: "billion", bn: "billion", t: "trillion" };
const UNITS = { ms: "milliseconds", "µs": "microseconds", us: "microseconds", ns: "nanoseconds", min: "minutes",
  h: "hours", hrs: "hours", kb: "kilobytes", mb: "megabytes", gb: "gigabytes", tb: "terabytes",
  rps: "requests per second", qps: "queries per second", tps: "transactions per second", ghz: "gigahertz",
  mhz: "megahertz", kbps: "kilobits per second", mbps: "megabits per second", gbps: "gigabits per second" };
const NUM = String.raw`\d+(?:,\d{3})*(?:\.\d+)?`;
const esc = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function num(n) {
  n = n.replace(/,/g, "");                                   // 50,000 -> 50000
  if (n.includes(".")) { const [w, f] = n.split("."); return `${w} point ${f.split("").join(" ")}`; }
  return n;
}

export function speakable(text) {
  let t = String(text);
  const cur = Object.keys(CURRENCY).sort((a, b) => b.length - a.length).map(esc).join("|");
  t = t.replace(new RegExp(`(${cur})\\s?(${NUM})\\s?(bn|[kKmMbBtT](?![a-zA-Z])|thousand|million|billion|trillion)?`, "g"),
    (_, c, n, sc) => `${num(n)}${sc ? " " + (SCALE[sc.toLowerCase()] || sc) : ""} ${CURRENCY[c]}`);
  t = t.replace(new RegExp(`\\b(${NUM})(bn|[kKmMbB])\\b`, "g"), (_, n, sc) => `${num(n)} ${SCALE[sc.toLowerCase()]}`);
  const units = Object.keys(UNITS).sort((a, b) => b.length - a.length).map(esc).join("|");
  t = t.replace(new RegExp(`\\b(${NUM})\\s?(${units})\\b`, "gi"), (_, n, u) => `${num(n)} ${UNITS[u.toLowerCase()]}`);
  t = t.replace(new RegExp(`\\b(${NUM})s\\b(?!\\w)`, "g"), (_, n) => `${num(n)} seconds`);
  t = t.replace(/\/s\b/g, " per second");
  t = t.replace(new RegExp(`\\b(${NUM})x\\b`, "g"), (_, n) => `${num(n)} times`);
  t = t.replace(/\bp(\d{2,3}(?:\.\d+)?)\b/g, (_, n) => `p ${num(n)}`);
  t = t.replace(new RegExp(NUM, "g"), (m) => num(m));
  t = t.replace(/(\d)\s?[-–]\s?(\d)/g, "$1 to $2").replace(/~/g, "about ").replace(/≈/g, "about ");
  return t.replace(/\s{2,}/g, " ").trim();
}
