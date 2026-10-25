"""The game contract (HLD §3): how the harness drives and observes every generated game.

- :mod:`~iterplay.contract.state`: the ``window.iterplay`` harness API and ``getState()`` schema
- :mod:`~iterplay.contract.templates`: the kinds of game iterplay builds (a 2D platformer, for
  now) and what each one changes
- :mod:`~iterplay.contract.controls`: each template's fixed control scheme
- :mod:`~iterplay.contract.url`: URL parameters every game must honour
- :mod:`~iterplay.contract.layout`: the files a generated game may contain

The Builder and Fixer prompts restate this contract and the smoke test enforces it.
"""
