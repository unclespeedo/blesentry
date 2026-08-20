# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Notifier seam — chat alert delivery and inbound command intake.

``Notifier`` (ADR-0002) is the single interface the rest of blesentry
uses to reach the operator: outbound alerts via :meth:`Notifier.send`
and inbound commands via :meth:`Notifier.commands`. Concrete backends
(``TelegramNotifier`` for production, ``MockNotifier`` for CI,
``NullNotifier`` for the ``none`` config) are config-selected and never
imported by name outside this package.
"""
