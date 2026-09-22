# Drift checks

Two heavy CUJs live here: **does the repo still obey its own policy**, and **what
consumes this config key**. Both run only when the caller explicitly asks.

Report findings. Never fix them.

---

## Step zero: scope to the committed state

**Do this first or every finding is suspect.** Every check below walks the working
tree, and you answer for the committed state. Those differ in two ways, and both
produce findings that look exactly like real drift.

```bash
git status --porcelain                 # any output = the tree differs from the commit
git ls-files '<path>' | head -1        # empty = untracked, so not part of the repo
git show 'HEAD:<path>' >/dev/null 2>&1 # succeeds = the file really is in the commit
```

**Untracked.** A scratch directory with an ignored `.venv` and no `manifest.yaml` is
indistinguishable from a broken recipe. Discard the finding, or say plainly that it is
local to the caller's machine.

**Tracked but locally modified.** The subtler one, and the reason `git ls-files` alone
is not enough: a tracked file the caller has edited or deleted in their working tree
produces a finding *worded identically* to a genuine one. Delete a recipe's `README.md`
locally and the validator reports "README.md is missing" — true of their disk, false of
the repo. When the tree is dirty, either confirm the specific finding against the commit
with `git show`, or say which findings could not be separated from local edits.

Telling an admin their repo is broken when it is that admin's own working tree is the
single worst failure this skill can produce. It costs one `git status` to avoid.

---

## Check 1 — the repo's own validators

Fast enough for any question (well under a second on the current repo).

```bash
uv run validate all
```

Covers manifest schema, structure (required files and dirs, size, naming), README, and
placement. Scope it with `uv run validate all core` or a single recipe path.

Registered subcommands live in `tools/validate.py` under `SUBCOMMANDS`; run
`uv run validate` with no arguments for the current set rather than assuming this list.

**Note what placement does and does not cover.** It answers "is this recipe in the right
folder" for the roots it inspects. A recipe duplicated at an unexpected depth may pass
every check while still being wrong — compare `git ls-files '*/manifest.yaml'` against
the expected `<root>/<language|vertical>/<recipe>` shape when that is the question.

---

## Check 2 — the language list, across every source

The language set is duplicated across every source the script below reads, and nothing
enforces agreement. This is the highest-value consistency check in the repo.

```bash
uv run --with pyyaml python3 -c "
import json, re, yaml
policy = yaml.safe_load(open('.github/policy.yml'))
schema = json.load(open('.github/schemas/manifest-schema.json'))
validate_manifest_src = open('tools/validate_manifest.py').read()
codeowners_src = open('.github/CODEOWNERS').read()

# Keys are lowercase dotted 'file.thing' paths, so the MISSING FROM line below
# names where to go and not just what is wrong.
sources = {
  'schema.language_enum': set(schema['properties']['language']['enum']),
  'policy.required_files': set(policy['required_files']['by_language']),
  'policy.required_dirs': set(policy['required_dirs']['by_language']),
  'policy.excluded_paths': set(policy['excluded_paths']) - {'common'},
  # findall over the quoted strings, not a comma split: the set may wrap across
  # lines or carry a trailing comma, either of which leaves newlines and an
  # empty string among the names and makes every comparison miss.
  'validate_manifest.language_namespace_dirs': set(re.findall(r'[\"\\']([a-z]+)[\"\\']', re.search(r'LANGUAGE_NAMESPACE_DIRS\s*=\s*\{([^}]*)\}', validate_manifest_src).group(1))),
  'codeowners.recipe_roots': set(re.findall(r'/(?:core|contrib)/([a-z]+)/', codeowners_src)),
}
for language in sorted(set().union(*sources.values())):
    absent_from = [name for name, langs in sources.items() if language not in langs]
    print(f'{language:12} {\"OK\" if not absent_from else \"MISSING FROM: \" + \", \".join(absent_from)}')
"
```

A language present in the schema but absent from `validate_manifest.py` is accepted by
schema validation and rejected by the validator — the confusing half-supported state the
add-a-language runbook exists to prevent. Dependabot ecosystems are deliberately not in
this check: the mapping from language to ecosystem is not one-to-one.

---

## Check 3 — values inlined instead of resolved

Any file that restates a number owned by `.github/policy.yml` will eventually contradict
it. Search docs and skills for policy-shaped claims:

```bash
grep -rnE "[0-9]+ ?(files|MB|days)" docs/ .agents/skills/ --include="*.md"
```

Review each hit: is it resolving the value from source, or asserting it? Known live
example — `.agents/skills/generate-manifest/SKILL.md` inlines the size limits and states
that `skills/` has none, while `policy.yml` defines them.

---

## Check 4 — dead pointers

Skills and docs route people to files by path. A renamed file turns a router into a dead
end silently, because only `docs/` links are covered by `docs-links.yml`.

A path is resolved **relative to the file that names it**, then relative to the repo
root — `SKILL.md` writes `reference/runbooks.md` meaning its own sibling, and testing
that from the root reports every one of them missing.

The pattern requires a `/`, so a bare `SKILL.md` or `__init__.py` — named as a kind of
file rather than as a pointer to one — does not become a finding.

```bash
grep -rnoE '`[.a-zA-Z0-9_-]+(/[.a-zA-Z0-9_-]+)+\.(md|py|ya?ml|json)`' \
  .agents/skills/ --include="*.md" \
  | tr -d '`' | while IFS=: read -r src _ f; do
      # f1-3 is `.agents/skills/<skill>`, which is exact because the grep root
      # above fixes the depth. Change that root and this must change with it.
      skill_root="$(printf '%s' "$src" | cut -d/ -f1-3)"
      [ -e "$f" ] || [ -e "$skill_root/$f" ] || [ -e "$(dirname "$src")/$f" ] \
        || echo "MISSING: $f  (named in $src)"
    done | sort -u
```

Expect false positives, and know the shape of them before you read the output: most
hits are **recipe-relative** paths — a runnability test, an agent module, an example
entry point — naming a file inside a recipe being described, not a file in this repo.
Every skill that documents recipe layout produces several. Fenced examples do the same.

(Those examples are deliberately unquoted here. Backtick them and this check reports
its own explanation as a finding.)

The check is a review aid, not a gate. Skim for a path that was clearly meant to point
at something in *this* repo, and ignore the rest.

---

## Check 5 — catalogue versus reality

Sort both sides. The catalogue is in reading order, `ls` is alphabetical, and comparing
them unsorted reports every skill as a difference.

```bash
diff <(ls -1 .agents/skills/ | sort) \
     <(grep -oE '^### `[a-z-]+`' docs/recipe-handbook/skills-catalog.md | tr -d '#` ' | sort)
```

**On disk, not in the catalogue** — invisible to anyone reading the docs. A real
finding.

**In the catalogue, not on disk** — read before reporting. It may be a broken promise,
or the catalogue may be describing a skill that is deliberately not a repo skill: one
installed globally by the assistant rather than shipped here. Say which it is; the fix
differs completely.

Neither case is CI-enforced.

---

## Check 6 — ownership that has gone stale

```bash
grep -rn "TODO: Replace with your" --include="manifest.yaml" core/ contrib/ skills/
```

`tools/validate_manifest.py` already fails on the literal placeholder strings, so hits
here mean a recipe that cannot be passing validation. Separately, a `poc` naming someone
who has left is invisible to every check — worth reporting when the caller asks who owns
something and the answer looks wrong.

---

## Check 7 — CODEOWNERS pointing at nothing

CODEOWNERS patterns contain globs, so the existence test has to expand them. Quoting
`"$p"` makes the shell look for a literal `*` and report every wildcard rule as
missing.

```bash
grep -oE '^/[^ ]+' .github/CODEOWNERS | sed 's|/\*\*$||; s|^/||' \
  | while read -r p; do
      # Unquoted on purpose: this is the expansion. `nullglob` makes a pattern
      # that matches nothing expand to zero words rather than to itself.
      #
      # BOTH tests are needed. A path with no wildcard is never expanded, so
      # it survives as one literal word and `$#` is 1 whether or not it
      # exists. Counting alone therefore passes every non-glob path — which
      # is most of the file — and the check silently finds nothing.
      ( shopt -s nullglob; set -- $p; [ "$#" -gt 0 ] && [ -e "$1" ] ) \
        || echo "NO SUCH PATH: $p"
    done
```

A rule for a directory that does not exist is harmless but misleading — it reads as
though the language is supported.

---

## Check 8 — labels the policy depends on but the repo does not have

`.github/policy.yml` names labels that must exist on GitHub for the sweeps and their
escape hatches to work. Nothing enforces that they do, and a missing one fails
silently: an exemption naming a nonexistent label exempts nothing, and a maintainer
reaching for a documented escape hatch finds it is not there.

This is the one check that reaches outside the repo, so it has to prove its instrument
works before believing its own output. A naive version asks `gh` for the labels and
treats whatever comes back as the truth — and when `gh` is missing or simply not
logged in, the empty result reads as *every label is missing*. That is a confident,
totally false report of a broken repo. The guards below are not defensive padding;
without them this check is worse than not running it.

```bash
uv run --with pyyaml python3 -c "
import shutil, subprocess, sys, yaml

LABEL_LIMIT = 500

if shutil.which('gh') is None:
    sys.exit('SKIPPED: gh is not installed, so labels cannot be checked.')

result = subprocess.run(
    ['gh', 'label', 'list', '--limit', str(LABEL_LIMIT), '--json', 'name',
     '--jq', '.[].name'],
    capture_output=True, text=True)
if result.returncode != 0:
    first_line = (result.stderr.strip().splitlines() or ['unknown error'])[0]
    sys.exit(f'SKIPPED: gh could not list labels ({first_line}).')

# splitlines, never split: a label may contain spaces ('good first issue'),
# and whitespace splitting would shred it into words that match nothing.
have = {line.strip() for line in result.stdout.splitlines() if line.strip()}
if not have:
    sys.exit('SKIPPED: gh returned no labels at all — treating that as a tool failure.')
if len(have) >= LABEL_LIMIT:
    sys.exit(f'SKIPPED: hit the {LABEL_LIMIT}-label ceiling, so the list may be '
             'truncated and a present label could read as missing. Raise LABEL_LIMIT.')

stale_policy = yaml.safe_load(open('.github/policy.yml'))['stale_policy']

# exempt_labels may be absent, null, or a bare string; unpacking any of those
# with * yields a TypeError or a set of single characters.
exempt = stale_policy['issues'].get('exempt_labels') or []
if isinstance(exempt, str):
    exempt = [exempt]

want = {label for label in (
            stale_policy.get('stale_label'),
            stale_policy.get('keep_open_label'),
            stale_policy.get('pull_requests', {}).get('bot_label'),
            *exempt,
        ) if label}
for label in sorted(want):
    print(f'{label:20} {\"OK\" if label in have else \"MISSING FROM REPO\"}')
"
```

Three failure states, three distinct outcomes: `gh` absent, `gh` present but not
authenticated, and `gh` answering with nothing. All three exit non-zero with `SKIPPED`
and report no findings at all. **Report a skip as a skip.** An audit that cannot run
this check is missing one check; an audit that reports its own tooling failure as
missing labels has lied to an admin.

This covers the labels `policy.yml` holds as *values*. At least one more is named only
inside a comment — the frozen-paths escape hatch — so also grep the file for
label-shaped names before calling the check complete.

Report a missing label as a repo problem, not a policy problem: the fix is almost
always to create the label rather than to edit the policy. Note also that a label the
canary creates on its first run is absent until then, which is expected rather than
broken.

---

## Check 9 — governance the routing table does not cover

This skill is designed to absorb most repo changes without being edited: it lists
directories rather than their contents, so a new workflow, repo skill, policy key,
label or recipe is found by looking, not by having been written down here.

What it cannot absorb is a *new kind* of thing — a governance file in a location no
routing row mentions. That failure is silent: the oracle answers "the repo doesn't
specify that" while the answer sits in a file it was never pointed at.

```bash
# Top-level and .github config that a routing row should account for.
ls -1 .github/*.yml .github/*.yaml .github/*.md 2>/dev/null
ls -1 *.toml *.json *.yml *.yaml .golangci.yml 2>/dev/null
ls -1 docs/ docs/recipe-handbook/
```

For each result, confirm `SKILL.md` names it, or names the directory it lives in.
Anything unaccounted for is a routing gap — report it as a change this skill needs,
not as a repo problem.

**Directory-level coverage is not enough for `docs/`.** Every page there is a distinct
routing target, and the value of a route is sending the caller to the right *page*, not
the right folder. Naming `docs/recipe-handbook/` does not cover the individual pages
inside it — check them one by one. This loophole hid a real gap once: the routing table
sent "what do the repo skills do" to `ls .agents/skills/` and never named
`skills-catalog.md`, and this check passed anyway.

**Templated routes cover a family, and a literal search cannot see it.** The routing
table has a row for a per-language handbook page written with a placeholder, which
covers every such page at once. A grep for the real filenames reports all of them
missing. Read the table before believing that kind of hit.

Run this after any change to the repo's own configuration, and expect it to fire
rarely: adding a fifth workflow of an existing kind is absorbed, adding the repo's
first `renovate.json` is not.

---

## Check 10 — every SKILL.md frontmatter still parses

A skill whose frontmatter is invalid YAML does not load, and nothing says so: no CI job
validates it, and the assistant simply behaves as though the skill was never written.
The classic cause is a bare `:` inside an unquoted description — `answered, not obeyed:
it says…` is enough — which is why most skills here use a folded block (`description: >`)
instead of a plain scalar.

```bash
uv run --with pyyaml python3 -c "
import yaml, pathlib
for f in sorted(pathlib.Path('.agents/skills').glob('*/SKILL.md')):
    try:
        d = yaml.safe_load(f.read_text().split('---')[1])
        assert isinstance(d, dict) and d.get('name') and d.get('description')
        print('OK  ', f.parent.name)
    except Exception as e:
        print('FAIL', f.parent.name, '-', e)
"
```

This has fired for real: `repo-oracle`'s own description was committed with a bare colon
and stopped loading entirely, while every other check in this file still passed. Run it
after touching any frontmatter, and prefer the folded block for anything long enough to
contain punctuation.

---

## Tracing what consumes a config key

The blast-radius question. Given a key such as `deployability.min_google_adk`:

1. **Search the dotted path**, for consumers reading it through `load_policy.py`:
   ```bash
   grep -rn "deployability.min_google_adk" . --exclude-dir=.git --exclude-dir=.venv
   ```
2. **Search the leaf key name alone**, for code that loads the YAML and subscripts it:
   ```bash
   grep -rn "min_google_adk" . --exclude-dir=.git --exclude-dir=.venv
   ```
3. **Search the literal value**, which is what finds the dangerous case — somewhere that
   hardcoded the number instead of reading it. These are the hits that turn "edit one
   line" into a multi-file change.
4. **Check every consumer family**: `.github/workflows/`, `.github/scripts/`,
   `tools/`, and `.agents/skills/` — plus `docs/`, which should link rather than restate.

**Never filter the search by file extension.** `.github/CODEOWNERS` has none, and an
extension-filtered search silently omits it. That mistake was made while building this
skill.

Report consumers grouped by family, and say plainly whether the change is confined to
`policy.yml` or reaches code.
