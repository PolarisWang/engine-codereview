# Lark Base API — Verified Patterns

Discovered during integration testing (2026-04-04). All patterns verified
against the live API.

## Base Creation

```bash
# Subcommand is +base-create (NOT +create)
lark-cli base +base-create --name "代码审查跟踪"
# Returns: data.base.base_token, data.base.url
```

## Table Creation

```bash
# Create table (--name required, --fields optional but rate-limited)
lark-cli base +table-create --base-token "$app_token" --name "代码审查记录"
# Returns: table_id in hint if name already exists
```

**Rate limit warning**: `+table-create --fields [...]` with many fields hits
`OpenAPIAddField` rate limit. Create tables empty, then add fields one-by-one
with 2s delays.

## Field Creation

```bash
# Flag is --json (NOT --field)
lark-cli base +field-create --base-token "$app_token" --table-id "$table_id" \
  --json '{"field_name":"Name","type":"text"}'
```

### Field Types (string names, NOT numeric IDs)

| Type String | Purpose | Notes |
|-------------|---------|-------|
| `text` | Plain text, URLs | Use `"style":{"type":"url"}` for clickable URL fields |
| `number` | Integers, decimals | |
| `select` | Single select | `"options":[{"name":"A"},{"name":"B"}]` (top-level, NOT under `property`) |
| `datetime` | Date/time | Default format: `yyyy/MM/dd` |
| `checkbox` | Boolean | |
| `link` | Table-to-table ref | Requires `"link_table":"tbl..."` — NOT for URLs |
| `attachment` | File upload | |
| `auto_number` | Auto-increment | Default ID field uses this |
| `created_at` | Auto timestamp | |
| `updated_at` | Auto timestamp | |
| `user` | User reference | |
| `formula` | Computed | |
| `lookup` | Cross-table lookup | |

### Select Field Options Format

Options go **top-level** in the JSON, not nested under `property`:
```json
{"field_name":"Status","type":"select","options":[{"name":"REVIEWING"},{"name":"APPROVED"}]}
```

NOT this (fails):
```json
{"field_name":"Status","type":"select","property":{"options":[...]}}
```

## Record Operations

**IMPORTANT**: `--json` takes flat field values — NO `"fields"` wrapper.

```bash
# Create/update record (use +record-upsert, flat JSON — no "fields" wrapper)
lark-cli base +record-upsert --base-token "$app_token" --table-id "$table_id" \
  --json '{"Ticket ID":"RAGE-12469","Status":"REVIEWING"}'

# Get record (1.0.48: record-get/record-list default to markdown — pass --format json)
lark-cli base +record-get --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json

# List records
lark-cli base +record-list --base-token "$app_token" --table-id "$table_id" --format json

# Delete record (requires --yes for safety)
lark-cli base +record-delete --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --yes
```

## Table/Field Management

```bash
# List tables
lark-cli base +table-list --base-token "$app_token"

# List fields
lark-cli base +field-list --base-token "$app_token" --table-id "$table_id"

# Delete table (requires --yes)
lark-cli base +table-delete --base-token "$app_token" --table-id "$table_id" --yes
```

## Common Pitfalls

- `+create` does not exist → use `+base-create`
- `--field` flag does not exist → use `--json`
- `--app-token` does not exist → use `--base-token`
- `--records` (plural) does not exist → use `+record-upsert --json`
- `{"fields":{...}}` wrapper in `--json` is rejected → pass flat `{"Field":"value"}` directly
- `"type":"url"` does not exist as a field type → use `"type":"text"` with `"style":{"type":"url"}`
- URL field values: use markdown link format `[display text](url)` for readable clickable links
  e.g. `"[RAGE-12469](https://jira.boomingtechs.cn/browse/RAGE-12469)"`
- `"type":"single_select"` does not exist → use `"type":"select"`
- `"type":"date_time"` does not exist → use `"type":"datetime"`
- Numeric type IDs (1, 2, 3...) are rejected → use string names
- Select `options` under `property` wrapper is rejected → put at top level