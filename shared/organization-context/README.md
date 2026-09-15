# Optional organization context provider

NightFalcon works without organization context. This directory contains an empty public provider so no private controls or standards ship in the package. Multi-tenant, BOLA, and isolation analysis always runs independently.

To add local controls, copy this directory outside the package, edit `_graph.json` to match `schema.json`, and set `NIGHTFALCON_CONTEXT_ROOT` to that directory. The loader must resolve every referenced note beneath the configured root; symlink or traversal escapes fail closed.

Provider fields use generic terms: `controls`, `platform_services`, `standards`, and `fingerprint_index`. Control IDs use `CTRL-NNNN`; standard IDs use `STD-NNNN`. Private provider content should remain outside public source control.

