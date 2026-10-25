// Smoke-test fixture: a page that loads cleanly but never defines window.iterplay. The ready
// check fails, so the state-schema and movement checks are skipped; no errors are logged.
document.body.textContent = "This build forgot the window.iterplay harness API.";
