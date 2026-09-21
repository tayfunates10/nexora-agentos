# ADR 0043: Console design system, themes and a bilingual interface

Status: implemented.

## Context

The console grew one page at a time. Each route carried its own markup and its own English
copy, navigation was a back link per page, and the single stylesheet encoded one dark
palette in literal hex values. Three requirements landed on that surface at once:

- a light and a dark theme, driven by the reference control-centre design;
- a persistent navigation shell with the seven workspace modules;
- Turkish and English, with Turkish as the product default.

Localising copy that lives inside JSX is not a translation problem. The meaning the
platform depends on — default-deny, "queued is not finished", "selection is not execution",
"only the requester may read an answer", "the call that crosses the limit is the last one
allowed" — is written in those sentences, and a sentence assembled from fragments cannot be
carried into another language without losing a condition. The same applies to numbers: an
accounting unit stored as exact integer micros must not pass through a float to be grouped,
and a recorded UTC instant must not drift because the interface language changed.

## Decision

### One DOM, two themes

Colour, elevation and border are CSS custom properties on `:root`. Light is the base; the
dark values are applied both under `prefers-color-scheme: dark` (guarded by
`:root:not([data-theme="light"])`) and under an explicit `:root[data-theme="dark"]`.

The server resolves the reader's stored choice and renders `data-theme` only for an
explicit light or dark. "System" renders no attribute at all, so the stylesheet's own media
query decides and the first paint already matches the first client render. There is no
theme-deciding inline script, so there is nothing to flash and nothing to mismatch during
hydration. Only colour, border and shadow transition, and only after the first frame:
geometry never animates, so a theme switch cannot reflow the page under the pointer.

### Language is resolved per request, never per process

`tr` and `en` are an allowlist. A cookie or `Accept-Language` value is resolved against that
tuple before anything is loaded, and the dictionaries are a static record rather than a
lookup by file name, so no header can select a module by path. Preference order is: a stored
valid choice, then the first supported `Accept-Language` preference (quality-weighted, with
`q=0` treated as a refusal), then Turkish.

Nothing caches a locale in module scope. Two concurrent requests in different languages are
served their own copy.

### Whole sentences, typed keys, real plurals

`messages/en.ts` defines the key space; `messages/tr.ts` is typed `Record<MessageKey, string>`,
so a key added to one language and not the other fails the type check.

Messages are complete sentences. A sentence containing a link keeps a `{link}` slot and each
language places it in its own word order, rather than being glued together from a lead, a
label and a tail. Plurals and counts go through a small ICU MessageFormat subset parsed into
a tree — arguments, `plural` with exact `=n` matches and `#`, and `select`. Apostrophes are
never ICU escapes, because in Turkish they are letters.

### What is never translated

API enums, identifiers, trace and run IDs, failure codes, provider and model names, server
keys, source keys, JSON arguments, citations, and everything a person or a model wrote —
workspace and agent names, instructions, task text, source titles and content, policy
reasons, judge rationales and raw failed output. Labels are translated; values are rendered
as stored. Raw technical evidence is shown under its own heading so it is never mistaken for
interface copy.

### Formatting follows the language; the value does not

Dates are formatted with `Intl` and stay in UTC, labelled UTC, in both languages. Accounting
units keep their exact integer-micros representation: the whole part is grouped by the
language's rules and joined with its decimal separator, without a float in the path. The
editable limit field carries no thousands separator, because it would be ambiguous against
the decimal mark the two languages swap. A quality delta is reported in percentage points,
never re-read as a relative percentage.

### Motion cannot hide content

The entrance and route transitions are CSS animations, which always settle on the visible
frame, so content is visible even when JavaScript never runs. The reveal-on-scroll hidden
state exists only under `html[data-motion="on"]`, an attribute set by a pre-paint script
that also honours `prefers-reduced-motion`. Without JavaScript the attribute is never set
and nothing is hidden.

## Consequences

- Permissions, CSRF tokens, idempotency keys, form action paths and input names are
  unchanged. The API remains the only authority: the console hides a control the reader
  cannot use, and never treats that as the check.
- The URL space is unchanged. There is no `/tr` or `/en` prefix, so OIDC callbacks, form
  actions and redirects keep working, and a shared link keeps its filters and cursor.
- Switching language re-renders the current route in place. The path, query filters, cursor,
  open record, scroll position and anything typed into a form survive it; no POST is
  replayed and no queued work is started again.
- A page cannot ship untranslated text: an automated gate reads every component and fails on
  literal words in JSX or in a text attribute, alongside key parity, placeholder parity,
  plural rendering and a check that no Turkish message is still its English source.
- Fonts are served from the platform's own stack rather than a downloaded family. The
  repository carries no font assets and the console must not depend on a third-party font
  host; the stack chosen covers Turkish diacritics. Substituting a licensed Inter is a
  local change to one custom property.
