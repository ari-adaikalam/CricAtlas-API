"""The API's own data refresh: Cricsheet archives in, Parquet export out, all under api/data/.

It does not import the dataset pipeline and does not read the repository's data/ folder. The only exception is the one-time
player profile migration (snapshot.py), which is documented in api/docs/internal/refresh_workflow.md.
"""
