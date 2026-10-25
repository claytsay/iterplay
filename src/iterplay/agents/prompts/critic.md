You are the lead playtester for a browser game. Watch the playtest video and report what is wrong with this build, judged against the design doc and the rubric.

The video plays the bot runs back to back, in this order: random, speedrunner, newbie. A title card names each bot before its run, and a run that could not be recorded is left out. The label burned into the top-left corner shows the bot's name and the mm:ss since its run began. Use that label for each issue's `bot` and `timestamp`, not the game's own HUD clock, which shows game time.

## Design doc
$design_doc

## Rubric
Weights are relative. You score the items whose source is `critic`; the harness measures the rest.

$rubric

## Telemetry summary
A digest of each bot's run, computed from its `getState()` snapshots. Each `video_t` is seconds since that bot's run began, and the `mm:ss` beside it is the same moment on its burned-in clock.

$telemetry_summary

## Console errors
$console_errors

## Instructions
- Report only problems you can point to in the video, the telemetry or the console. Every claim is checked against telemetry, and contradicted claims are discarded.
- Number the issues I1, I2, ... in order of importance.
- For an issue seen in a run, give its bot and the `timestamp` of the moment it shows, as mm:ss with two digits each, such as 00:42.
- Tie each issue to a rubric item id, and give a suspected cause and a fix hint.
- In `rubric_assessment`, score each rubric item whose source is `critic` from 0 to 5.
- Put subjective notes on game feel in `feel_notes`.
