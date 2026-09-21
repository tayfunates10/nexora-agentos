import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { en, type MessageKey } from "../messages/en.ts";
import { tr } from "../messages/tr.ts";
import { dictionary, createUi, translate } from "../lib/i18n/messages.ts";
import {
  formatMessage, messagePlaceholders, parseMessage, MessageSyntaxError,
} from "../lib/i18n/icu.ts";
import {
  DEFAULT_LOCALE, LOCALES, baseLanguage, isLocale, negotiateLocale, resolveLocale,
} from "../lib/i18n/locale.ts";
import { isTheme, resolveTheme, themeAttribute } from "../lib/i18n/theme.ts";

const keys = Object.keys(en) as MessageKey[];

/**
 * Messages whose two languages are legitimately identical: a product name, a brand word,
 * a dash, or a pattern made only of placeholders and units the platform keeps verbatim.
 * Anything not listed here and still identical is an untranslated string.
 */
const IDENTICAL_BY_DESIGN = new Set<string>([
  "common.productName", "common.brand", "common.brandSuffix", "common.empty",
  "health.service.postgres.name", "health.service.redis.name",
  "evaluations.passedOfTotal", "judge.scoreOfFour",
  "format.milliseconds", "format.microsUnit",
  // Terms this product keeps identical in Turkish on purpose.
  "runs.column.agent", "runDetail.steps.model", "spend.column.model",
]);

/** Sample values for every variable a message declares, typed the way it uses them. */
function sampleValues(message: string, count: number): Record<string, string | number> {
  return Object.fromEntries(messagePlaceholders(message)
    .map(({ name, kind }) => [name, kind === "plural" ? count : "sample"]));
}

test("both dictionaries carry exactly the same keys", () => {
  assert.deepEqual(Object.keys(tr).sort(), keys.slice().sort());
  assert.equal(keys.length, Object.keys(tr).length);
  // A duplicated literal key would silently drop a message, so the parsed object has to
  // be as large as the source file's key list.
  for (const locale of LOCALES) assert.equal(Object.keys(dictionary(locale)).length, keys.length);
});

test("no message is empty, a placeholder for later work, or its own key", () => {
  for (const locale of LOCALES) {
    const messages = dictionary(locale);
    for (const key of keys) {
      const value = messages[key];
      assert.equal(typeof value, "string", `${locale}:${key} is not a string`);
      assert.notEqual(value.trim(), "", `${locale}:${key} is empty`);
      assert.equal(value, value.trim(), `${locale}:${key} has stray whitespace`);
      assert.doesNotMatch(value, /\b(TODO|FIXME|TBD|XXX)\b/i, `${locale}:${key} is unfinished`);
      assert.notEqual(value, key, `${locale}:${key} renders its own key`);
    }
  }
});

test("a Turkish message identical to its English source is reported", () => {
  const untranslated = keys.filter(key => tr[key] === en[key] && !IDENTICAL_BY_DESIGN.has(key));
  assert.deepEqual(untranslated, [], "these keys were never translated");
});

test("every message parses and carries the same variables in both languages", () => {
  for (const key of keys) {
    const english = messagePlaceholders(en[key]);
    const turkish = messagePlaceholders(tr[key]);
    // Same names and same types: a count used as a plural in one language cannot become
    // a bare substitution in the other.
    assert.deepEqual(turkish, english, `${key} has mismatched placeholders`);
    for (const placeholder of english) {
      assert.notEqual(placeholder.kind, "conflict", `${key} uses ${placeholder.name} two ways`);
    }
  }
});

test("plural messages render for every count that reaches them", () => {
  const plurals = keys.filter(key => messagePlaceholders(en[key]).some(p => p.kind === "plural"));
  assert.ok(plurals.length > 0, "the suite must cover at least one plural");
  for (const key of plurals) {
    for (const locale of LOCALES) {
      for (const count of [0, 1, 2, 5, 100]) {
        const message = dictionary(locale)[key];
        const rendered = formatMessage(message, sampleValues(message, count), locale);
        assert.notEqual(rendered.trim(), "", `${locale}:${key} rendered nothing for ${count}`);
        assert.doesNotMatch(rendered, /[{}#]/, `${locale}:${key} left syntax in the output`);
      }
    }
  }
});

test("interpolated messages substitute real values in both languages", () => {
  const tricky = "İğdır Çalışma Alanı <script>";
  for (const locale of LOCALES) {
    const rendered = translate(locale, "agents.startWith", { agentName: tricky });
    assert.ok(rendered.includes(tricky), `${locale} dropped the agent name`);
    assert.doesNotMatch(rendered, /\{|\}/);
  }
  // A long name and an empty one both stay substitutions, never a crash.
  assert.ok(translate("tr", "agents.startWith", { agentName: "" }).length > 0);
  assert.ok(translate("en", "agents.startWith", { agentName: "x".repeat(300) }).length > 300);
});

test("the message parser refuses syntax it cannot render faithfully", () => {
  for (const broken of [
    "{count, plural, one {x}}",          // no `other` branch
    "{count, plural, one {x} other {y}", // unterminated
    "{ }",                               // unnamed placeholder
    "{value, number}",                   // an unsupported argument type
    "unbalanced }",
  ]) {
    assert.throws(() => parseMessage(broken), MessageSyntaxError, `accepted: ${broken}`);
  }
  // Apostrophes are letters in Turkish, never ICU escapes.
  assert.equal(formatMessage("Nexora'da {n} kayıt", { n: 2 }, "tr"), "Nexora'da 2 kayıt");
});

test("language negotiation honours stored choice, then quality weights, then Turkish", () => {
  assert.equal(resolveLocale("en", "tr"), "en");
  assert.equal(resolveLocale("de", "en-GB,en;q=0.9"), "en");
  assert.equal(resolveLocale(null, null), DEFAULT_LOCALE);
  assert.equal(resolveLocale(undefined, "de,fr"), DEFAULT_LOCALE);
  // Weights decide the order, and q=0 is a refusal rather than a preference.
  assert.equal(negotiateLocale("tr;q=0.4, en;q=0.9"), "en");
  assert.equal(negotiateLocale("tr;q=0, en"), "en");
  assert.equal(negotiateLocale("en;q=0, tr"), "tr");
  assert.equal(negotiateLocale("de-DE, fr;q=0.8"), null);
  assert.equal(negotiateLocale("*"), DEFAULT_LOCALE);
  assert.equal(negotiateLocale(""), null);
  // Region variants resolve to the language this platform serves.
  assert.equal(baseLanguage("tr-TR"), "tr");
  assert.equal(baseLanguage("en-US"), "en");
  assert.equal(baseLanguage("de-AT"), null);
});

test("only an allowlisted locale or theme is ever accepted", () => {
  for (const attack of ["../../etc/passwd", "en.json", "TR", "", "__proto__"]) {
    assert.equal(isLocale(attack), false, `accepted locale ${attack}`);
    assert.equal(resolveLocale(attack, null), DEFAULT_LOCALE);
    assert.equal(isTheme(attack), false, `accepted theme ${attack}`);
    assert.equal(resolveTheme(attack), "system");
  }
  // "system" renders no attribute, so the stylesheet's own media query decides and the
  // server output matches the first client paint.
  assert.equal(themeAttribute("system"), undefined);
  assert.equal(themeAttribute("dark"), "dark");
});

test("a missing message is fatal outside production", () => {
  assert.throws(() => translate("tr", "nope.not.a.key" as MessageKey));
  assert.equal(createUi("tr").locale, "tr");
  assert.equal(createUi("en").tag, "en-GB");
});

// ---------------------------------------------------------------------------
// Raw user-visible text must come from the dictionary, not from a page. The scan is a
// gate, not a proof: it reads the rendered text of every component and refuses literal
// words that never passed through a message key.
// ---------------------------------------------------------------------------
const TEXT_ATTRIBUTES = /\s(?:aria-label|placeholder|title|alt|aria-description)="([^"]*)"/g;
// JSX text between two tags, on one line. Anything carrying code punctuation is a type
// annotation or an expression rather than something a reader sees, so it is skipped.
const JSX_TEXT = />([^\n<>{}();:=,|&?"`]+)</g;
const HAS_WORDS = /[A-Za-zÇĞİÖŞÜçğıöşü]{2,}/;

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap(entry => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return path.endsWith(".tsx") ? [path] : [];
  });
}

function withoutComments(source: string): string {
  return source
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

test("no user-visible text is written into a page instead of a dictionary", () => {
  const offences: string[] = [];
  for (const path of [...sourceFiles("app"), ...sourceFiles("components")]) {
    const source = withoutComments(readFileSync(path, "utf8"));
    for (const [, text] of source.matchAll(JSX_TEXT)) {
      if (HAS_WORDS.test(text)) offences.push(`${path}: ${text.trim()}`);
    }
    for (const [, text] of source.matchAll(TEXT_ATTRIBUTES)) {
      if (HAS_WORDS.test(text)) offences.push(`${path}: attribute "${text}"`);
    }
  }
  assert.deepEqual(offences, [], "these strings never reach a translator");
});
