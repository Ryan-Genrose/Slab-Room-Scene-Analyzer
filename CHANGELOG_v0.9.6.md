# Room Scene Analyzer v0.9.6

## Reference Library refresh reliability

- Fixed the apparent freeze that could occur at exactly **20 scanned materials** during `REFRESH LIBRARY`.
- Root cause: v0.9.5 performed a synchronous Google Cloud Storage cache upload every 20 completed records. A slow or stalled GCS request could block the Streamlit run at that exact checkpoint.
- The refresh loop now writes a fast **local checkpoint every 20 records** without waiting on cloud storage.
- Cloud checkpoints now happen less frequently (**every 100 records**) and use a short, bounded timeout with retries disabled, so a cloud hiccup cannot stop the scan.
- The final cloud-cache save also has a bounded timeout. If it fails, the refresh completes and shows a warning instead of hanging indefinitely.
- Cloud checkpoint/save errors are retained in the cache diagnostics for troubleshooting.

No new dependencies or secrets are required.
