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
never loads ``mock`` (mirrors ``blesentry.notifier``). Offline
replay (``blesentry.detection.replay``, ``blesentry replay``) feeds
historical windows through the same ``observe`` surface without
touching the scan loop or the outbox. Canonical eval feature
vectors (F3) live in ``blesentry.detection.features`` and read
those windows; they are not a Detector backend. The A1 approach
trigger (ADR-0007) lives in ``blesentry.detection.approach`` —
a predicate over F3 slope/span. The A2 online tracker
(``blesentry.detection.trajectory``) feeds that predicate from
bounded per-identity deques. The A3 backend
(``blesentry.detection.approach_detector``) is the
``[detection] backend = "approach"`` union member; this package
``__init__`` does not re-export it. The C1 crowd spec (ADR-0008)
lives in ``blesentry.detection.crowd`` — band-count helpers and
frozen CUSUM knobs; C4 is the ``crowd`` backend. The I1 inside spec
(ADR-0009) lives in ``blesentry.detection.inside`` — adjacent-count
helpers and frozen sustain knobs; I2 wires
:func:`~blesentry.detection.inside.build_inside_excluded` and own-rotating-
gear queries; I3 is the ``inside`` backend
(``blesentry.detection.inside_detector.InsideDetector``).
The F6 familiar baseline (``blesentry.detection.familiar``) builds
the K-day ``is_familiar`` allow-list at startup and daily refresh;
I2 wires it into detectors.
"""
