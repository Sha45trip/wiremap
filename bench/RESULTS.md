# Benchmark results

| repo | files | routes | calls | wires | flags | scan s |
|---|---|---|---|---|---|---|
| fastapi-template | 139 | 23 | 0 | 0 | 45 | 4.1 |
|  | hub_function: 4, missing_auth: 7, no_error_handling: 11, unused_endpoint: 23 | | | | | |
| redash | 549 | 10 | 111 | 5 | 318 | 14.0 |
|  | missing_auth: 1, no_error_handling: 95, no_timeout: 111, orphan_call: 90, unresolvable_url: 16, unused_endpoint: 5 | | | | | |
| superset | 4714 | 4 | 25 | 0 | 72 | 119.5 |
|  | no_error_handling: 20, no_timeout: 23, orphan_call: 2, unresolvable_url: 23, unused_endpoint: 4 | | | | | |

## Flag totals across corpus

- no_timeout: 134
- no_error_handling: 126
- orphan_call: 92
- unresolvable_url: 39
- unused_endpoint: 32
- missing_auth: 8
- hub_function: 4
