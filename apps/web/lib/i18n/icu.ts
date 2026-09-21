import { LOCALE_TAGS, type Locale } from "./locale.ts";

/**
 * A deliberately small ICU MessageFormat subset: simple arguments, `plural` with exact
 * `=n` matches and the `#` count, and `select`. It is a real parser rather than a string
 * replacement, so a plural in one language cannot silently degrade into literal text in
 * the other.
 *
 * Apostrophes are never treated as ICU escapes. Turkish copy uses them as letters
 * ("Nexora'da"), and eating them would corrupt the interface language this exists to
 * serve. Literal braces are therefore not expressible, which no message needs.
 */
export type MessageNode =
  | { type: "text"; value: string }
  | { type: "argument"; name: string }
  | { type: "plural"; name: string; options: Record<string, MessageNode[]> }
  | { type: "select"; name: string; options: Record<string, MessageNode[]> };

export type MessageValues = Record<string, string | number>;

export class MessageSyntaxError extends Error {}

const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const OPTION = /^(=\d+|[A-Za-z_][A-Za-z0-9_]*)$/;

class Parser {
  private index = 0;
  private readonly source: string;

  constructor(source: string) { this.source = source; }

  parse(): MessageNode[] {
    const nodes = this.parseNodes(false);
    if (this.index < this.source.length) throw new MessageSyntaxError("Unbalanced '}'");
    return nodes;
  }

  private parseNodes(nested: boolean): MessageNode[] {
    const nodes: MessageNode[] = [];
    let text = "";
    while (this.index < this.source.length) {
      const character = this.source[this.index];
      if (character === "}") {
        if (!nested) break;
        this.index += 1;
        break;
      }
      if (character === "{") {
        if (text) { nodes.push({ type: "text", value: text }); text = ""; }
        nodes.push(this.parsePlaceholder());
        continue;
      }
      text += character;
      this.index += 1;
    }
    if (text) nodes.push({ type: "text", value: text });
    return nodes;
  }

  private parsePlaceholder(): MessageNode {
    this.index += 1; // consume "{"
    const name = this.readUntil([",", "}"]).trim();
    if (!NAME.test(name)) throw new MessageSyntaxError(`Invalid placeholder name: ${name}`);
    if (this.source[this.index] === "}") { this.index += 1; return { type: "argument", name }; }
    this.index += 1; // consume ","
    const kind = this.readUntil([",", "}"]).trim();
    if (kind !== "plural" && kind !== "select") {
      throw new MessageSyntaxError(`Unsupported placeholder type: ${kind}`);
    }
    if (this.source[this.index] !== ",") throw new MessageSyntaxError(`${kind} needs options`);
    this.index += 1; // consume ","
    const options: Record<string, MessageNode[]> = {};
    while (true) {
      this.skipWhitespace();
      if (this.source[this.index] === "}") { this.index += 1; break; }
      const option = this.readUntil(["{"]).trim();
      if (!OPTION.test(option)) throw new MessageSyntaxError(`Invalid ${kind} option: ${option}`);
      if (this.source[this.index] !== "{") throw new MessageSyntaxError(`Missing body for ${option}`);
      this.index += 1; // consume "{"
      options[option] = this.parseNodes(true);
    }
    // Every plural and select carries `other`, so an unexpected value still renders text.
    if (!options.other) throw new MessageSyntaxError(`${kind} is missing an 'other' option`);
    return { type: kind, name, options };
  }

  private readUntil(stops: string[]): string {
    const start = this.index;
    while (this.index < this.source.length && !stops.includes(this.source[this.index])) {
      this.index += 1;
    }
    if (this.index >= this.source.length) throw new MessageSyntaxError("Unterminated placeholder");
    return this.source.slice(start, this.index);
  }

  private skipWhitespace(): void {
    while (this.index < this.source.length && /\s/.test(this.source[this.index])) this.index += 1;
  }
}

const cache = new Map<string, MessageNode[]>();

export function parseMessage(message: string): MessageNode[] {
  const cached = cache.get(message);
  if (cached) return cached;
  const parsed = new Parser(message).parse();
  cache.set(message, parsed);
  return parsed;
}

/** The variables a message consumes, with the type each usage demands. */
export function messagePlaceholders(message: string): { name: string; kind: string }[] {
  const found = new Map<string, string>();
  const walk = (nodes: MessageNode[]) => {
    for (const node of nodes) {
      if (node.type === "text") continue;
      const kind = node.type === "argument" ? "string" : node.type;
      // A name used both plainly and as a plural is a contract mismatch, not a merge.
      const existing = found.get(node.name);
      found.set(node.name, existing && existing !== kind ? "conflict" : kind);
      if (node.type !== "argument") Object.values(node.options).forEach(walk);
    }
  };
  walk(parseMessage(message));
  return [...found].map(([name, kind]) => ({ name, kind })).sort((a, b) => a.name.localeCompare(b.name));
}

function renderNodes(
  nodes: MessageNode[], values: MessageValues, locale: Locale, current: number | null,
): string {
  let output = "";
  for (const node of nodes) {
    if (node.type === "text") {
      output += current === null ? node.value : node.value.replaceAll("#", formatCount(current, locale));
      continue;
    }
    const value = values[node.name];
    if (node.type === "argument") {
      // A missing value renders its own name rather than "undefined"; the CI gate is what
      // keeps that from shipping, and the reader still sees which field is absent.
      output += value === undefined
        ? `{${node.name}}`
        : typeof value === "number" ? formatCount(value, locale) : value;
      continue;
    }
    if (node.type === "select") {
      const option = node.options[String(value)] ?? node.options.other;
      output += renderNodes(option, values, locale, current);
      continue;
    }
    const count = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(count)) throw new MessageSyntaxError(`plural '${node.name}' needs a number`);
    const exact = node.options[`=${count}`];
    const category = new Intl.PluralRules(LOCALE_TAGS[locale]).select(count);
    const option = exact ?? node.options[category] ?? node.options.other;
    output += renderNodes(option, values, locale, count);
  }
  return output;
}

function formatCount(value: number, locale: Locale): string {
  return new Intl.NumberFormat(LOCALE_TAGS[locale]).format(value);
}

export function formatMessage(message: string, values: MessageValues, locale: Locale): string {
  return renderNodes(parseMessage(message), values, locale, null);
}
