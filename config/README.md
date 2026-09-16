# Configuration

The repository ships examples only. Create local files before starting:

```powershell
Copy-Item config/companies.example.yaml config/companies.yaml
Copy-Item config/candidate_profile.example.yaml config/candidate_profile.yaml
Copy-Item config/rag_sources.example.yaml config/rag_sources.yaml
```

`candidate_profile.yaml` contains personal information and is ignored by Git.
`companies.yaml` can be replaced by the source-discovery workflow after the
first successful run. Paths in `rag_sources.yaml` are resolved locally; never
point them at folders that should not be indexed.

