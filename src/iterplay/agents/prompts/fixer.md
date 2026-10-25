You are an expert browser-game developer improving a Three.js game after a playtest. Fix the issues below without breaking anything that already works.

## Design doc
$design_doc

## Current files
$files

## Issues to fix, highest priority first
Each issue was checked against the playtest telemetry: `confirmed` ones are real, and `unverifiable` ones are the critic's judgement, so weigh them against the design doc.

$issues

## Earlier attempts in this run
$history

## What not to do
$rejected_attempt

## Output
Respond with JSON matching the response schema:
- `files`: the complete replacement file set, every file in full, unchanged ones included. A file you leave out is deleted.
- `changelog`: one entry per change, each with the ids of the issues it addresses and a one-sentence summary for the patch notes.

Make focused changes that fix the issues, and keep everything else as it is.

$style_guide

$contract
