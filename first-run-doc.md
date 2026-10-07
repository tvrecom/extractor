First run succeeded

The sequence was:

* Started at 14:25:13 with `language=en-US`, `region=US`. 
* Enumeration fetched 10 pages and discovered 198 new IDs. Since TMDB returns 20 results/page, this is essentially the expected ~200 IDs after deduplication.
* Extraction was capped at 100 titles, exactly matching the configured `TMDB_MAX_DETAILS_PER_RUN=100`. The documentation says this is the normal per-run bound. 
* All 100 extraction attempts succeeded. There were zero failed, retryable, exhausted, or 404/"gone" titles.
* One title, TMDB ID `1156869`, produced a warning because its genres were empty. It was still emitted rather than failed, which matches the documented validation behavior for warnings.  
* No TV/episode extraction happened. Every extracted record shown is a movie, and the summary reports `episodes: 0`.
* 98 discovered titles remain pending because 198 were discovered and only 100 were extracted.
* The crawler checkpoint is now `movie:popularity page=11`, so the next invocation should continue from page 11 rather than restart. 
* Runtime was only 9.13 seconds.
* Overall status was explicitly `ok`, with `errors: []`.

The important thing is that this was not an accidental early termination. The architecture is intentionally bounded: each invocation enumerates at most 10 pages and extracts at most 100 titles, then saves its checkpoint and exits. 

One potentially confusing field is:

`windows_completed: 0`

That does not mean enumeration failed. The run is still traversing the popularity window; it has advanced to page 11 but hasn't finished that window yet. The summary also says `frontier_exhausted: false`, `windows_split: 0`, and `errors: []`.

So, in short:

**198 discovered → 100 extracted successfully → 98 queued for future runs → 1 warning → 0 actual failures → checkpoint saved at popularity page 11.**

The run behaved exactly like the documented resumable/bounded crawler is designed to behave.
