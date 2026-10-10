# Sandbox test-run journal

Record each Atira rehearsal here as soon as it is started. This makes the live
platform recording, its package, the Git/Entire checkpoint, and the hypothesis
easy to reconnect later.

| Started (Berlin) | Commit | Run ID | Hypothesis / change | Result and follow-up |
| --- | --- | --- | --- | --- |
| 2026-10-10 | `024f8b3` | `run_3c3568bb65f5871e` (`reh_3462880dc545db9e`) | Baseline after action state verification, extraction schema gate, pre-submit review, and recovery safeguards. | Finished eligible at quality 70.7 in 18:23. Missing/incorrect position inputs and total consistency were the main score losses; repeated extraction timeouts and a post-confirmation token 401 were also observed. |
| 2026-10-10 | `e1ecb2c` | `run_23a00fcc468b92db` (`reh_4fd47a36fcec6187`) | Copy explicit units from clarifications, trust only recognized unit labels, require reference/positions/total on the review page, and remove the repeated task brief from extraction prompts. | Finished eligible at quality 22.8 in 14:11. Clarification units were extracted correctly, but reasoning-enabled extraction still had a 2:35 p95 and timed out on several pages; the resulting partial bid reached the form, then the run token expired during entry. This was the diagnostic variant that motivated disabling reasoning for schema-constrained transcription. |
| 2026-10-10 | `b8f3228` | `run_60233f96d79c14a0` (`reh_8bfdeb8c7420e1cc`) | Same unit and review safeguards, with reasoning disabled only for typed evidence extraction. | Finished eligible at quality 98.7 in 10:07 (model-call p95 12s). All bid positions were present; the pre-submit checklist reported READY with the review values visible. The final confirmation click was followed by an expired-token 401 before a receipt could be read, but Atira completed scoring the answer. Follow up on post-submit confirmation handling without weakening the checklist. |
| 2026-10-10 | `f0afce0` + uncommitted navigation recovery | `run_cf7ab2ecf10b18f3` | Bound browser navigation waits to 5s; attempt refresh, then replace the tab in the same browser slot and retry once. | Failed with no answer after 19:09 (0 quality, not eligible). The live trace confirmed that a timed-out tender navigation reopened the tab and loaded the login page, demonstrating recovery; the run later hit a 120s submit-agent model timeout and did not complete. Package SHA-256: `d6998b83630d`. Local full suite passed all 242 tests after the final source edits. The run package predates the later click/form-submit navigation timeout coverage, so rerun before drawing a score comparison. |

Local validation for `b8f3228`: `uv run --no-sync python -m unittest discover tests -v` passed all 239 tests. Focused case-file and preflight tests also passed after the extraction-speed change.

Use `atira status` to find a run ID, `atira logs <run-id> --follow` for its
console trace, and `atira package <run-id>` to inspect exactly what the sandbox
received. Keep the platform's score/breakdown and the next decision in the last
column after it finishes.
