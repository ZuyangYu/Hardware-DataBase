# external-skill-safety-scanner

Local acceptance fixture built from a real public Agent Skill. It is **not** a
first-party HDB Skill and it has **not** been signed or attested by the upstream
publisher.

## Upstream source

- Repository: <https://github.com/SkillsCatalog/registry>
- Path: `skills/skill-safety-scanner`
- Commit: `e83c630d59313e1899255ca0bb9046abcb6e66cc`
- License: MIT (see `LICENSE`)

The following files are copied unchanged from the upstream commit:

| File | SHA-256 |
|---|---|
| `SKILL.md` | `8bc4b067e561793b149fa365a3f28f1d3b2197b68b258fcd0cda6fdb3f879b31` |
| `MANIFEST.json` | `a0e67c089a5ac5ad59831b7fcbf90aa56d30053d1e19e91a96b5b705a6c0858b` |
| `scripts/safety_scan.py` | `f1569b792ad748f492f3646ec426b56cbb1ff6c88f43b8b412377a70ed9791fd` |

`entrypoint.py` is an HDB Runtime adapter added for this fixture. It does not
modify the scanner. It only:

1. finds one read-only ZIP Artifact under `/work/in/assets`;
2. extracts it with path, symlink, file-count and size limits;
3. calls the upstream `scan_skill()` function;
4. writes `scan.json`, `summary.md` and Runtime Contract v1 `result.json`.

When no ZIP is attached it returns `needs_user_input` with
`artifact:target-zip`. Grade A/B maps to `completed`; lower grades map to
`validation_failed` with bounded issue objects.

## Local adapter run

```bash
HDB_SKILL_IN=/path/to/in/assets \
HDB_SKILL_OUT=/path/to/out \
PYTHONPATH=. \
python examples/skills/external-skill-safety-scanner/entrypoint.py
```

The input directory must contain exactly one `.zip` file. The ZIP may have
`SKILL.md` at its root or inside one top-level folder.

## Real Runner acceptance

The package was built with the Runner's fixed `/v1/skill-build` template and
executed with `--network none` as the non-root sandbox user. Observed results:

- clean target: `completed`, upstream grade A (100/100);
- target with a hardcoded password and `os.system`: `validation_failed`, grade F;
- self-scan of this upstream scanner: `validation_failed`, grade F;
- no input Artifact: `needs_user_input` for `artifact:target-zip`.

The self-scan result is expected from a pattern scanner: its own rule
definitions contain strings such as `eval(` and `os.system(`, and the scanner
does not exempt its own source. It is a demonstration of how
`validation_failed` is preserved, not a malware verdict.

## Trust boundary

This fixture has a declared upstream reference and hashes, but no Ed25519
signature from a key registered in HDB. Its provenance status is therefore
`not_present`, and the runtime gate keeps it on the restricted `profile/dev-l1`.
The generated SBOM is an HDB file/dependency inventory, not a third-party
attestation.
