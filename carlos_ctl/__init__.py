# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""carlos-ctl for the Debian/Ubuntu single-host CARLOS EMR deployment.

Shipped as its own Debian package, ``carlos-ctl`` (this repository,
github.com/carlos-emr/carlos-ctl), which the ``carlos-emr`` application
package depends on; through carlos-emr 2026.09.0~snapshot24 the same code
shipped inside carlos-emr. The two packages version independently: the CLI
is a driver over what carlos-emr installs (its Flyway migrations, helper
scripts, configuration skeletons and the OSCAR 19 import manifests under
/usr/share/carlos-emr/o19-manifest), so a schema change ships with
carlos-emr and never needs a CLI release. Every verb refuses with one
clear line when carlos-emr is not installed.

This package is the .deb counterpart of carlos-podman's ``carlos_ctl``
(project ``carlos-podman-ctl`` in github.com/carlos-emr/carlos-podman):
same command name, same language, and the same verb names wherever the two
deployments share a concept (``check``, ``db``, ``db-migrate``, ``db-users``,
``db-dump``, ``backup full|verify|status``, ``cert-renew``, ``rotate``,
``status``) — an operator moving between a podman site and a single-VM site
should not have to relearn the tool. Verbs that only make sense here
(``init-config``, ``waf``, ``destroy-data``, service lifecycle) live in their
own namespace and collide with nothing over there.

The module layout deliberately mirrors carlos-podman's carlos_ctl
(cli/util/config/dbops/validate) so that a future shared core — one package,
two runner backends — is a refactor, not a rewrite. Until then the two trees
are separate on purpose: this one drives systemd services and a host MariaDB
over the unix socket, that one drives rootless podman pods, and a premature
abstraction over those would be worse than the duplication. Unifying the
two trees in this repository is a separate, later issue.
"""

#: the release version; debian/changelog and the release tag carry the same
#: number (the release workflow refuses a tag that disagrees)
__version__ = "1.0.0"
