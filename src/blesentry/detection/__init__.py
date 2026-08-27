# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Detector seam — adaptive alerting backends (ADR-0006).

``Detector`` is the single interface the rest of blesentry will use
to turn a scan window into would-be alerts. Concrete backends
(``NullDetector`` for the ``none`` config, ``MockDetector`` for CI
and F1 replay; approach / crowd / inside later) are config-selected
and imported lazily by :func:`blesentry.config.build_detector` —
this package ``__init__`` stays docstring-only so the ``none`` path
never loads ``mock`` (mirrors ``blesentry.notifier``).
"""
