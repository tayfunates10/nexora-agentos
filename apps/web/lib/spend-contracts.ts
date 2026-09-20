import { z } from "zod";

const count = z.number().int().nonnegative();
// Micros are millionths of one unit of the operator accounting currency. The API
// never states a currency symbol, so neither does this console.
export const MICROS_PER_UNIT = 1_000_000;
export const MAX_LIMIT_MICROS = 1_000_000_000_000_000;
const micros = z.number().int().min(0).max(MAX_LIMIT_MICROS);

export const spendCategorySchema = z.enum(["agent_run", "evaluation_judge", "embedding"]);
export const enforcementSchema = z.enum(["enforce", "monitor"]);
export const MAX_ALERT_THRESHOLDS = 5;
// Offered in the console; a threshold set through the API is preserved alongside these.
export const STANDARD_THRESHOLDS = [50, 75, 90, 100] as const;
const threshold = z.number().int().min(1).max(100);
const thresholdList = z.array(threshold).max(MAX_ALERT_THRESHOLDS).refine(
  values => values.every((value, index) => index === 0 || values[index - 1] < value),
  "Alert thresholds must be sorted and unique",
);

export const spendAlertSchema = z.object({
  threshold_percent: threshold,
  monthly_limit_micros: micros,
  consumed_micros: micros,
  enforcement: enforcementSchema,
  created_at: z.iso.datetime({ offset: true }),
}).refine(
  // An alert records the crossing that happened: the stored amounts must show it.
  alert => alert.consumed_micros * 100 >= alert.threshold_percent * alert.monthly_limit_micros,
  "Invalid spend alert",
);

export const spendSummarySchema = z.object({
  workspace_id: z.uuid(),
  period_start: z.iso.datetime({ offset: true }),
  period_end: z.iso.datetime({ offset: true }),
  consumed_micros: micros,
  monthly_limit_micros: micros.nullable(),
  enforcement: enforcementSchema.nullable(),
  remaining_micros: micros.nullable(),
  exhausted: z.boolean(),
  alert_thresholds: thresholdList,
  alerts: z.array(spendAlertSchema),
  categories: z.array(z.object({
    category: spendCategorySchema,
    call_count: count.positive(),
    input_tokens: count,
    output_tokens: count,
    cost_micros: micros,
  })),
}).refine(
  summary => summary.period_start < summary.period_end
    // A ledger period total that disagrees with its own breakdown is not evidence.
    && summary.categories.reduce((total, row) => total + row.cost_micros, 0) === summary.consumed_micros
    && (summary.monthly_limit_micros === null) === (summary.enforcement === null)
    && (summary.monthly_limit_micros === null
      ? summary.remaining_micros === null && !summary.exhausted
      : summary.remaining_micros === Math.max(0, summary.monthly_limit_micros - summary.consumed_micros))
    && (!summary.exhausted || summary.enforcement === "enforce"),
  "Invalid spend summary",
);

export const budgetSchema = z.object({
  workspace_id: z.uuid(),
  monthly_limit_micros: micros,
  enforcement: enforcementSchema,
  alert_thresholds: thresholdList,
  updated_at: z.iso.datetime({ offset: true }),
});

export const spendRecordSchema = z.object({
  id: z.uuid(),
  source_key: z.string().min(1).max(200),
  category: spendCategorySchema,
  provider: z.string().min(1).max(64),
  model: z.string().min(1).max(128),
  input_tokens: count,
  output_tokens: count,
  cost_micros: micros,
  occurred_at: z.iso.datetime({ offset: true }),
});

export const spendRecordPageSchema = z.object({
  items: z.array(spendRecordSchema),
  next_cursor: z.uuid().nullable(),
});

export type SpendSummary = z.infer<typeof spendSummarySchema>;
export type SpendAlert = z.infer<typeof spendAlertSchema>;
export type SpendRecord = z.infer<typeof spendRecordSchema>;
export type SpendCategory = z.infer<typeof spendCategorySchema>;

export const CATEGORY_LABELS: Record<SpendCategory, string> = {
  agent_run: "Agent runs",
  evaluation_judge: "Quality judge",
  embedding: "Embeddings",
};

// Operators think in accounting units, the ledger stores exact micros. Both
// conversions use integer arithmetic so a budget is never off by a rounding step.
const UNITS = /^(\d{1,10})(?:[.,](\d{1,6}))?$/;

export function unitsToMicros(value: string): number {
  const match = UNITS.exec(value.trim());
  if (!match) throw new Error("Invalid amount");
  const total = BigInt(match[1]) * BigInt(MICROS_PER_UNIT) + BigInt((match[2] ?? "").padEnd(6, "0"));
  if (total > BigInt(MAX_LIMIT_MICROS)) throw new Error("Amount is too large");
  return Number(total);
}

export function microsToUnits(value: number): string {
  const total = BigInt(value);
  const whole = total / BigInt(MICROS_PER_UNIT);
  const fraction = (total % BigInt(MICROS_PER_UNIT)).toString().padStart(6, "0");
  return `${whole}.${fraction.replace(/(\d\d)(\d*?)0*$/, "$1$2")}`;
}

export function formatUnits(value: number): string {
  const [whole, fraction] = microsToUnits(value).split(".");
  return `${Number(whole).toLocaleString("en-GB")}.${fraction}`;
}

export function formatMicros(value: number): string {
  return `${value.toLocaleString("en-GB")} micros`;
}

export function spendTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
  }).format(new Date(value)) + " UTC";
}

export function spendPeriod(summary: SpendSummary): string {
  const format = new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium", timeZone: "UTC",
  });
  // The period end is exclusive; label the last day operators actually see.
  const lastDay = new Date(Date.parse(summary.period_end) - 86_400_000);
  return `${format.format(new Date(summary.period_start))} – ${format.format(lastDay)} UTC`;
}

export function parseThresholds(values: string[]): number[] {
  const parsed = values.map(value => {
    if (!/^\d{1,3}$/.test(value)) throw new Error("Invalid threshold");
    const percent = Number(value);
    if (percent < 1 || percent > 100) throw new Error("Invalid threshold");
    return percent;
  });
  const unique = [...new Set(parsed)].sort((a, b) => a - b);
  if (unique.length > MAX_ALERT_THRESHOLDS) throw new Error("Too many thresholds");
  return unique;
}

export function offeredThresholds(configured: number[]): number[] {
  // A threshold an operator set through the API stays on screen, so saving the
  // form cannot silently drop it.
  return [...new Set([...STANDARD_THRESHOLDS, ...configured])].sort((a, b) => a - b);
}

export function usedRatio(summary: SpendSummary): number | null {
  if (summary.monthly_limit_micros === null) return null;
  if (summary.monthly_limit_micros === 0) return 1;
  return Math.min(1, summary.consumed_micros / summary.monthly_limit_micros);
}
