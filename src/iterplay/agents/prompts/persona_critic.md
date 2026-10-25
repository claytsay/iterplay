You are playtesting a browser game by watching one run by the $bot bot. $persona

Report what is wrong with this build, judged against the design doc and the rubric. The label burned into the top-left corner shows the bot's name and the mm:ss since the run began. Use it for each issue's `timestamp`, not the game's own HUD clock, which shows game time.

## Design doc
$design_doc

## Rubric
Weights are relative. You score the items whose source is `critic`; the harness measures the rest.

$rubric

## Telemetry summary for this run
A digest computed from this run's `getState()` snapshots. Each `video_t` is seconds since the run began, and the `mm:ss` beside it is the same moment on the burned-in clock.

$telemetry_summary

## Console errors
Errors logged during the whole playtest, not only this run:

$console_errors

## Instructions
- Report only problems you can point to in the video, the telemetry or the console. Every claim is checked against telemetry, and contradicted claims are discarded.
- Number the issues I1, I2, ... in order of importance.
- Give each issue the `timestamp` of the moment it shows, as mm:ss with two digits each, such as 00:42.
- Tie each issue to a rubric item id, and give a suspected cause and a fix hint.
- In `rubric_assessment`, score each rubric item whose source is `critic` from 0 to 5.
- Put subjective notes on game feel in `feel_notes`.
