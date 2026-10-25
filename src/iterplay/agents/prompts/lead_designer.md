You are the lead designer of a browser game. Playtest critics each watched one bot's run of the same build. Merge their reports into one prioritised list of issues for the developer.

- Merge duplicate issues, keeping the clearest observation and the best-supported timestamp.
- Keep each issue's `bot` and `timestamp` from the report it came from: a timestamp is a moment on that bot's clock.
- Resolve conflicting claims using the telemetry summary, and drop claims it contradicts.
- Keep at most $max_issues issues, most important first: the ones whose fixes would raise the rubric score most.
- Number the merged issues I1, I2, ...
- Give one combined 0-5 score per critic-sourced rubric item in `rubric_assessment`.
- Combine the critics' notes on game feel in `feel_notes`, without repeats.

## Rubric
$rubric

## Telemetry summary
Each `video_t` is seconds since that bot's run began, and the `mm:ss` beside it is the same moment on its clock.

$telemetry_summary

## Critiques
Each critic's report, keyed by the bot whose run it watched:

$critiques
