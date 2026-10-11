# Invalid runtime contract examples

These documents are deliberately invalid. Do not use them as project templates.
`tests/test_runtime_schemas.py` checks rejection by the Draft 2020-12 schemas
and by the corresponding native runtime reader.

| File | Deliberate defect |
| --- | --- |
| `control-config.json` | A boolean parameter declares an unsupported numeric bound. |
| `diagnostic-intent.json` | CPU budget is negative. |
| `execution-model.json` | An event declares both an explicit resource and rank routing. |
| `io-request.json` | A permit request omits its participants. |
| `query-source.json` | Coverage uses text instead of a boolean. |
| `runtime-request.json` | Expiration is zero. |
| `scientific-field.json` | An axis has zero size. |
| `site-capabilities.json` | Permission uses text instead of a boolean. |
| `summary-query.json` | Median is outside the supported query family. |

Valid examples are in [examples/runtime](../../../../examples/runtime).
