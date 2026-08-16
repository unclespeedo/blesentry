# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import blesentry


def test_package_version_exported() -> None:
    """The package exports a non-empty version string."""
    assert blesentry.__version__
