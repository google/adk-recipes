<!-- word count: 619 (target 500, cap 800) -->

# Docs Style

Rules for every doc under `docs/`. Applies to human edits and
AI-generated edits equally. If a change violates a rule, fix the
change.

## Structure

- Prefer claims over topics when a heading can do work. "Ownership"
  is a topic; "Every recipe declares a team owner" is a claim.
  Short topic labels are fine for section markers ("Naming",
  "Ruff") when they index content the reader is scanning for.
- Every sentence adds information absent from the heading. If a
  sentence can be deleted without loss, delete it.
- No section intros or outros. Heading → content. No "In this
  section we'll cover..." or "That's all!"
- Tables and lists win over prose for 3+ items.
- Show the CLI, don't describe it. Code block over prose.

## Voice

- Lead with the imperative or the fact. "Add `manifest.yaml`." not
  "You'll want to make sure you add..."
- Active voice. "CI validates the manifest." not "The manifest is
  validated by CI."
- One example, not three. If one doesn't communicate, fix the
  writing.
- Prefer plain English over idioms and jargon. A non-native reader
  should not have to pause on phrases like "escape hatch", "green
  CI", or "tweak."

## Banned words

Do not use these unless the qualifier is load-bearing:

- Fillers: `simply`, `just`, `basically`, `essentially`, `note
  that`, `keep in mind`
- Hedges: `generally`, `usually`, `typically`, `in most cases`
- Weasels: `comprehensive`, `robust`, `seamless`, `powerful`,
  `intuitive`, `modern`, `cutting-edge`
- Signposts: `as we saw above`, `as discussed later`, `the
  following section`

## Callouts

`> Note:` blocks are for warnings and gotchas. Not for padding. If
it could be a sentence, make it a sentence.

## Word budgets

Every doc declares its current word count in an HTML comment on line
1:

    <!-- word count: 285 (target 300, cap 500) -->

Update the count when you edit. Cut before extending past the
target. Being under target is fine — it means the content fit; it
does not mean the doc is underdeveloped.

| File | Target | Cap |
|---|---|---|
| `recipe-checklist.md` | 400 | 600 |
| `recipe-handbook/README.md` | 500 | 800 |
| `recipe-handbook/anatomy.md` | 800 | 1200 |
| `recipe-handbook/manifest.md` | 700 | 1000 |
| `recipe-handbook/languages/python.md` | 700 | 1000 |
| `recipe-handbook/skills-catalog.md` | 800 | 1200 |
| `recipe-handbook/troubleshooting.md` | 500+, grows | no cap |

## Two-pass discipline

1. Draft: get the information down.
2. Cut: separate pass, deletion only, target 20% shorter. No adding
   or rephrasing allowed in this pass.

## Cross-links

Every handbook page ends with a two-link footer:

    ← [Checklist](../recipe-checklist.md) · [Handbook](./README.md)

Adjust the relative paths for pages under subdirectories
(e.g. `languages/` uses `../../recipe-checklist.md` and
`../README.md`).

**Non-handbook docs** (`recipe-checklist.md`,
`recipe-handbook/README.md`, `docs/README.md`) also carry a
footer for consistency, but link only where it makes sense — the
handbook README self-links to itself for symmetry.

## Link text

- Never include `.md` in visible link text.
- In prose, use natural language:
  "the [anatomy](./anatomy.md) page shows the shape of a recipe."
- In table-of-contents style bullet lists, use the page's title
  as the link text: `- [Anatomy of a recipe](./anatomy.md) — ...`
- For anchor links, name the section:
  the [size limits](./anatomy.md#size-limits) section of anatomy.
- Back-link footers use bare nouns: `← [Checklist] · [Handbook]`.

## Handbook page structure

- Handbook landing pages carry at most 4-5 content sections.
- Intent statements and standards belong together in one section,
  not split into separate ones.
- Table-of-contents style navigation goes at the end of a
  landing page, not the middle.
- Group TOCs by reading order ("start here" vs. "reference")
  when the reader has more than one path through.

---

← [Checklist](../docs/recipe-checklist.md) · [Handbook](../docs/recipe-handbook/README.md)
